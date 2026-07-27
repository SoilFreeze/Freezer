import streamlit as st
import pandas as pd
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates

st.set_page_config(layout="wide", page_title="Pipe Mapper Admin")

st.title("🗺️ As-Built Pipe Mapper")
st.markdown("Upload your site plan, type a pipe name, and click the map to log its X/Y coordinates.")

# 1. Initialize session memory to store our clicks
if 'mapped_pipes' not in st.session_state:
    st.session_state.mapped_pipes = pd.DataFrame(columns=['NodeNum', 'Map_X', 'Map_Y'])

col1, col2 = st.columns([3, 1])

with col2:
    st.subheader("1. Setup")
    uploaded_file = st.file_uploader("Upload As-Built Image (PNG/JPG)", type=["png", "jpg", "jpeg"])
    
    if uploaded_file:
        pipe_name = st.text_input("2. Pipe Name to Map (e.g., TP-0031):").upper().strip()
        
        st.subheader("3. Mapped Coordinates")
        # Display the table of pipes we've clicked so far
        st.dataframe(st.session_state.mapped_pipes, use_container_width=True)
        
        # Add a button to download the results as CSV so you can copy/paste to your Google Sheet
        if not st.session_state.mapped_pipes.empty:
            csv = st.session_state.mapped_pipes.to_csv(index=False)
            st.download_button("Download Coordinates CSV", data=csv, file_name="pipe_coordinates.csv", mime="text/csv")
            
            if st.button("Clear All Data"):
                st.session_state.mapped_pipes = pd.DataFrame(columns=['NodeNum', 'Map_X', 'Map_Y'])
                st.rerun()

with col1:
    if uploaded_file:
        img = Image.open(uploaded_file)
        
        if pipe_name:
            st.info(f"👆 Click on the map where **{pipe_name}** is located.")
        else:
            st.warning("⚠️ Enter a Pipe Name in the sidebar before clicking!")
            
        # 4. Render the interactive image
        # The image will output a dictionary like {'x': 150, 'y': 300} when clicked
        click_data = streamlit_image_coordinates(img, key="site_map")
        
        # 5. Process the click
        if click_data is not None and pipe_name:
            x_coord = click_data['x']
            y_coord = click_data['y']
            
            # Check if we already mapped this pipe, if so, update it. If not, add it.
            if pipe_name in st.session_state.mapped_pipes['NodeNum'].values:
                st.session_state.mapped_pipes.loc[st.session_state.mapped_pipes['NodeNum'] == pipe_name, ['Map_X', 'Map_Y']] = [x_coord, y_coord]
            else:
                new_row = pd.DataFrame({'NodeNum': [pipe_name], 'Map_X': [x_coord], 'Map_Y': [y_coord]})
                st.session_state.mapped_pipes = pd.concat([st.session_state.mapped_pipes, new_row], ignore_index=True)
            
            # Reset the text input for the next pipe
            st.rerun()
