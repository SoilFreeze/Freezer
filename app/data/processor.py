import pandas as pd
import sys
from google.cloud import bigquery
from google.oauth2 import service_account
from app.utils import config 
import re

# --- SAFE FRAMEWORK DETECTION ---
try:
    import streamlit as st
    # Smart detection: If Shiny is running the app, it will be in sys.modules.
    if 'shiny' in sys.modules:
        HAS_STREAMLIT = False
    else:
        HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

def safe_cache(ttl=600):
    """Uses Streamlit cache if available, otherwise just runs the function."""
    def decorator(func):
        if HAS_STREAMLIT:
            return st.cache_data(ttl=ttl)(func)
        return func
    return decorator

def safe_cache_resource():
    """Uses Streamlit cache_resource if available, otherwise just runs the function."""
    def decorator(func):
        if HAS_STREAMLIT:
            return st.cache_resource(func)
        return func
    return decorator

@safe_cache_resource()
def get_bq_client():
    from google.oauth2 import service_account
    from google.cloud import bigquery
    
    SCOPES = [
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
    
    try:
        # 1. If running in Streamlit, use st.secrets
        if HAS_STREAMLIT and "gcp_service_account" in st.secrets:
            info = st.secrets["gcp_service_account"]
            credentials = service_account.Credentials.from_service_account_info(
                info
            ).with_scopes(SCOPES)
            return bigquery.Client(credentials=credentials, project=info["project_id"])
            
        # 2. If running in Shiny, grab the JSON string from Environment Variables
        else:
            import os
            import json
            
            gcp_json_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
            
            if not gcp_json_str:
                print("❌ BQ Auth: 'GCP_SERVICE_ACCOUNT_JSON' environment variable is missing!")
                return None
                
            try:
                info = json.loads(gcp_json_str)
                credentials = service_account.Credentials.from_service_account_info(
                    info
                ).with_scopes(SCOPES)
                print("✅ BQ Auth: Successfully authenticated via Environment Variable.")
                return bigquery.Client(credentials=credentials, project=info["project_id"])
                
            except json.JSONDecodeError as e:
                print(f"❌ BQ Auth: The environment variable is not valid JSON. Error: {e}")
                return None
                
    except Exception as e:
        if HAS_STREAMLIT:
            st.error(f"❌ BigQuery Authentication Failed: {e}")
        else:
            print(f"❌ BigQuery Authentication Failed: {e}")
        return None
        
@safe_cache(ttl=600)
def get_universal_portal_data(project_id, lookback_days=35, is_summary_page=False, show_masked=False, show_baddata=False):
    client = get_bq_client()
    if client is None: return pd.DataFrame()
    
    root_job_id = str(project_id).split('-')[0].strip()

    # Build dynamic phase matching for the SQL query
    phase_sql = ""
    if not is_summary_page:
        phase_match = re.search(r'(?i)Phase\s*(\d+)', str(project_id))
        if phase_match:
            target_phase = phase_match.group(1)
            phase_sql = f"AND TRIM(CAST(Phase AS STRING)) = '{target_phase}'"

    # 1. Safely map exclusions without triggering syntax errors
    exclusions = ["'FALSE'"] 
    if not show_masked:
        exclusions.append("'MASKED'")
    if not show_baddata:
        exclusions.append("'BADDATA'")
    
    exclusion_str = ", ".join(exclusions)

    # 2. Lift the temperature bounds cleanly if hunting anomalies
    if show_baddata:
        temp_bounds_sql = "(1=1)" 
    else:
        temp_bounds_sql = "(m.temperature >= -30.0 AND m.temperature <= 120.0)"

    # 3. Smart Office Filter (Make sure this 'if' is pulled all the way to the left!)
    if 'OFFICE' in str(root_job_id).upper():
        office_filter_sql = ""
    else:
        office_filter_sql = "AND UPPER(CAST(m.Project AS STRING)) NOT LIKE '%OFFICE%' AND UPPER(CAST(m.Location AS STRING)) NOT LIKE '%OFFICE%'"

    # 4. Push the timeline filter directly into BigQuery
    time_filter_sql = f"AND m.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_days} DAY)"

    query = f"""
        WITH ProjectAssignments AS (
            SELECT 
                NodeNum, 
                TRIM(CAST(Location AS STRING)) as Reg_Location, 
                TRIM(CAST(Bank AS STRING)) as Reg_Bank, 
                Depth as Reg_Depth, 
                Phase as Reg_Phase, 
                System as Reg_System,
                
                COALESCE(
                    SAFE_CAST(Start_Date AS TIMESTAMP),
                    SAFE.PARSE_TIMESTAMP('%m/%d/%Y %H:%M:%S', CAST(Start_Date AS STRING)),
                    SAFE.PARSE_TIMESTAMP('%m/%d/%Y', CAST(Start_Date AS STRING)),
                    SAFE.PARSE_TIMESTAMP('%Y-%m-%d', CAST(Start_Date AS STRING)),
                    TIMESTAMP('2000-01-01')
                ) as active_start,
                
                COALESCE(
                    SAFE_CAST(End_Date AS TIMESTAMP),
                    SAFE.PARSE_TIMESTAMP('%m/%d/%Y %H:%M:%S', CAST(End_Date AS STRING)),
                    SAFE.PARSE_TIMESTAMP('%m/%d/%Y', CAST(End_Date AS STRING)),
                    SAFE.PARSE_TIMESTAMP('%Y-%m-%d', CAST(End_Date AS STRING)),
                    TIMESTAMP('2099-12-31')
                ) as active_end
                
            FROM `{config.NODE_REGISTRY_TABLE}`
            WHERE TRIM(SPLIT(CAST(Project AS STRING), '-')[OFFSET(0)]) = @root_job_id
              {phase_sql}
        )
        SELECT 
            m.Project as Raw_Project_Name,
            m.NodeNum,
            m.temperature,
            m.timestamp,
            COALESCE(NULLIF(v.Reg_Location, ''), m.Location, 'Unassigned') as Location,
            COALESCE(NULLIF(v.Reg_Bank, ''), m.Bank, '—') as Bank,
            COALESCE(v.Reg_Depth, m.Depth) as Depth,
            COALESCE(NULLIF(CAST(v.Reg_Phase AS STRING), ''), m.Phase) as Phase,
            COALESCE(NULLIF(v.Reg_System, ''), m.System) as System,
            m.Hardware,
            m.approval_status,
            m.BaseElevation,   
            m.node_elevation   
        FROM `{config.MASTER_VIEW}` m
        INNER JOIN ProjectAssignments v 
          ON UPPER(TRIM(CAST(m.NodeNum AS STRING))) = UPPER(TRIM(CAST(v.NodeNum AS STRING)))
          
          AND m.timestamp >= v.active_start
          AND m.timestamp <= v.active_end
          
        WHERE {temp_bounds_sql}
          AND m.Project LIKE CONCAT(@root_job_id, '%')
          {office_filter_sql}
          {time_filter_sql}
          AND UPPER(COALESCE(CAST(m.approval_status AS STRING), 'TRUE')) NOT IN ({exclusion_str})
        ORDER BY m.timestamp ASC
    """
    
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("root_job_id", "STRING", root_job_id)]
    )
       
    df = client.query(query, job_config=job_config).to_dataframe()
        
    return df


def apply_sanity_filter(df):
    """Flags physically impossible temperatures as BADDATA instead of deleting them."""
    if df.empty or 'temperature' not in df.columns:
        return df
        
    if 'approval_status' not in df.columns:
        df['approval_status'] = 'TRUE'
        
    extreme_mask = (df['temperature'] < -30.0) | (df['temperature'] > 120.0)
    df.loc[extreme_mask, 'approval_status'] = 'BADDATA'
    
    return df

@safe_cache(ttl=600)
def get_sensor_performance_data(project_id="All Projects"):
    """
    Calculates the health score and failure rate for sensors.
    Can be scoped to a single project or run globally across the fleet.
    """
    client = get_bq_client()
    if client is None: 
        return pd.DataFrame()
    
    project_filter = ""
    if project_id != "All Projects":
        root_job_id = str(project_id).split('-')[0].strip()
        project_filter = f"WHERE Project LIKE '{root_job_id}%'"
        
    query = f"""
        SELECT 
            NodeNum,
            MAX(Project) as Project,
            MAX(Location) as Location,
            COUNT(*) as Total_Readings,
            COUNTIF(UPPER(approval_status) = 'BADDATA') as Bad_Readings,
            COUNTIF(UPPER(approval_status) = 'MASKED') as Masked_Readings,
            ROUND((COUNTIF(UPPER(approval_status) = 'BADDATA') / COUNT(*)) * 100, 2) as Failure_Rate_Pct
        FROM `{config.MASTER_VIEW}`
        {project_filter}
        GROUP BY NodeNum
        HAVING Total_Readings > 10  
        ORDER BY Failure_Rate_Pct DESC, Bad_Readings DESC
    """
    
    try:
        df = client.query(query).to_dataframe()
        return df
    except Exception as e:
        if HAS_STREAMLIT:
            st.error(f"Failed to fetch performance data: {e}")
        else:
            print(f"Failed to fetch performance data: {e}")
        return pd.DataFrame()
