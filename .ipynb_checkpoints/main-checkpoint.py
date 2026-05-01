import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import requests
import pgeocode
import folium
from streamlit_folium import st_folium
import time
import math

# --- Configuration & Setup ---
st.set_page_config(page_title="Donor Exposome Mapper", layout="wide", page_icon="🫘")

# Nephrotoxic hazard multipliers based on EPA RSEI modeling principles
# Superfund sites carry a heavier multiplier due to historical, long-term multi-chemical exposure
TOXIN_WEIGHTS = {
    'TRI_ACTIVE': 2.5,     # Active reporting manufacturing
    'SUPERFUND_MIX': 6.0,  # Legacy National Priorities List (NPL) site
    'UNKNOWN': 1.0
}

# --- Backend Processing Class ---
class DonorExposomeMapper:
    def __init__(self):
        # Shifting to the EPA's dedicated spatial API to prevent 500 Server Errors
        self.epa_spatial_url = "https://ofmpub.epa.gov/frs_public2/frs_rest_services.get_facilities"

    def geocode_zip(self, zip_code):
        nomi = pgeocode.Nominatim('us')
        location = nomi.query_postal_code(zip_code)
        if pd.isna(location.latitude):
            return None, None
        return Point(location.longitude, location.latitude), location

    def fetch_radial_epa(self, lat, lon, radius_miles, program, status_placeholder, retries=3):
        """
        Uses the EPA FRS Spatial API to fetch only the facilities within the exact radius.
        Includes a retry/backoff mechanism for unstable government servers (HTTP 503/504)
        and strict inner error handling to prevent corrupted data from crashing the batch.
        """
        url = f"{self.epa_spatial_url}?latitude83={lat}&longitude83={lon}&search_radius={radius_miles}&pgm_sys_acrnm={program}&output=JSON"
        
        for attempt in range(retries):
            try:
                response = requests.get(url, timeout=20)
                
                # Handle temporary server overloads gracefully
                if response.status_code in [500, 502, 503, 504]:
                    status_placeholder.text(f"EPA {program} server overloaded (HTTP {response.status_code}). Retrying {attempt + 1}/{retries}...")
                    time.sleep(2) 
                    continue
                
                # Catch any other non-200 responses
                if response.status_code != 200:
                    st.warning(f"⚠️ EPA API Issue ({program}): Returned HTTP {response.status_code}.")
                    return []
                    
                data = response.json()
                facilities = []
                
                # The FRS API JSON structure embeds the data inside Results -> FRSFacility
                fac_list = []
                if 'Results' in data and 'FRSFacility' in data['Results']:
                    fac_list = data['Results']['FRSFacility']
                    # If only one facility exists, the API returns a dict instead of a list
                    if isinstance(fac_list, dict):
                        fac_list = [fac_list]
                elif 'FRSFacility' in data:
                    fac_list = data['FRSFacility']
                    if isinstance(fac_list, dict):
                        fac_list = [fac_list]
                elif isinstance(data, list):
                    fac_list = data
                    
                for item in fac_list:
                    # FRS Spatial API uses CamelCase keys
                    lat_val = item.get('Latitude83') or item.get('LATITUDE83')
                    lon_val = item.get('Longitude83') or item.get('LONGITUDE83')
                    name = item.get('FacilityName') or item.get('FACILITY_NAME') or 'Unknown Site'
                    
                    if not lat_val or not lon_val:
                        continue
                        
                    # FIX: Safely parse coordinates to prevent a single typo from crashing the batch
                    try:
                        lat_float = float(lat_val)
                        lon_float = float(lon_val)
                    except ValueError:
                        continue # Skip this specific corrupted facility
                        
                    facilities.append({
                        'FACILITY_NAME': name,
                        'LATITUDE': lat_float,
                        'LONGITUDE': lon_float,
                        'DATABASE': f"FRS_{program}",
                        'PRIMARY_TOXIN': 'SUPERFUND_MIX' if program == 'SEMS' else 'TRI_ACTIVE'
                    })
                    
                status_placeholder.text(f"Fetched {len(facilities)} {program} facilities within radius...")
                return facilities
                
            except requests.exceptions.Timeout:
                status_placeholder.text(f"EPA {program} server timed out. Retrying {attempt + 1}/{retries}...")
                time.sleep(2)
                continue
            except Exception as e:
                st.warning(f"⚠️ Error parsing FRS Spatial API for {program}: {e}")
                return []
        
        # If the loop finishes all retries without returning, the server is truly down.
        st.warning(f"⚠️ The EPA's {program} database is currently unresponsive. Proceeding with available data.")
        return []

    def compile_exposome_data(self, donor_point, radius_miles, progress_bar, status_placeholder):
        """Pulls spatial data natively, bypassing the need to download the entire state."""
        lat = donor_point.y
        lon = donor_point.x
        
        # 1. Fetch Active Industrial Sites (TRIS)
        status_placeholder.text("Querying EPA Spatial API for Active Toxics (TRIS)...")
        tri_data = self.fetch_radial_epa(lat, lon, radius_miles, 'TRIS', status_placeholder)
        progress_bar.progress(0.50)
        
        # 2. Fetch Historical/Abandoned/Superfund Sites (SEMS)
        status_placeholder.text("Querying EPA Spatial API for Historical Superfund Sites (SEMS)...")
        sems_data = self.fetch_radial_epa(lat, lon, radius_miles, 'SEMS', status_placeholder) 
        progress_bar.progress(0.90)
        
        combined_data = tri_data + sems_data
        
        if not combined_data:
            return gpd.GeoDataFrame()
            
        df = pd.DataFrame(combined_data)
        
        # Drop exact duplicates if a site happens to be in both databases
        df = df.drop_duplicates(subset=['LATITUDE', 'LONGITUDE'])
        
        status_placeholder.text(f"Successfully compiled {len(df)} environmental sites.")
        time.sleep(1) # Let the user read the success message
        progress_bar.progress(1.0)
        
        gdf = gpd.GeoDataFrame(
            df, 
            geometry=gpd.points_from_xy(df.LONGITUDE, df.LATITUDE),
            crs="EPSG:4326"
        )
        return gdf

    def calculate_exposome_score(self, donor_point, epa_gdf, buffer_miles=10):
        """Calculates a normalized 0-100 composite risk index based on spatial proximity and hazard weight."""
        if epa_gdf.empty:
            return 0.0, 0.0, pd.DataFrame()
            
        donor_gdf = gpd.GeoDataFrame([{'id': 1}], geometry=[donor_point], crs="EPSG:4326")
        
        donor_gdf = donor_gdf.to_crs("EPSG:3857")
        epa_gdf = epa_gdf.to_crs("EPSG:3857")
        
        buffer_meters = buffer_miles * 1609.34
        donor_buffer = donor_gdf.geometry.buffer(buffer_meters).iloc[0]
        
        sites_within_radius = epa_gdf[epa_gdf.geometry.intersects(donor_buffer)].copy()
        
        if sites_within_radius.empty:
            return 0.0, 0.0, pd.DataFrame()
        
        raw_idw_sum = 0
        sites_within_radius['DISTANCE_METERS'] = sites_within_radius.geometry.distance(donor_gdf.geometry.iloc[0])
        sites_within_radius['DISTANCE_MILES'] = sites_within_radius['DISTANCE_METERS'] / 1609.34
        
        for idx, row in sites_within_radius.iterrows():
            toxin = row['PRIMARY_TOXIN']
            weight = TOXIN_WEIGHTS.get(toxin, TOXIN_WEIGHTS['UNKNOWN'])
            
            # Floor distance to 0.1 miles to prevent division-by-zero singularities
            dist = max(row['DISTANCE_MILES'], 0.1) 
            
            site_score = weight / (dist ** 2)
            raw_idw_sum += site_score
            sites_within_radius.at[idx, 'RAW_RISK_CONTRIBUTION'] = round(site_score, 2)
            
        # Logarithmic normalization to create a 0-100 Composite Score
        # Scaling factor of 12 stretches the curve so a typical urban area falls around 40-60
        composite_score = min(100.0, round(math.log1p(raw_idw_sum) * 12.0, 1))
            
        return composite_score, round(raw_idw_sum, 2), sites_within_radius.drop(columns=['geometry', 'DISTANCE_METERS'])

# --- UI Layout ---

with st.sidebar:
    st.title("Navigation")
    page = st.radio("Go to:", ["Exposome Calculator", "Patient Education & Science"])
    
    st.markdown("---")
    st.subheader("About the Author")
    st.markdown("""
    **Neel Agarwal** *Medical Student, The Ohio State University College of Medicine* *neel.agarwal@osumc.edu*
    
    Focused on leveraging geospatial data and machine learning to improve surgical outcomes and organ evaluation in nephrology and urology.
    """)

mapper = DonorExposomeMapper()

if page == "Exposome Calculator":
    st.title("The Donor Exposome Nephrotoxin Mapper")
    st.markdown("Evaluate a deceased donor's cumulative environmental nephrotoxin exposure utilizing a normalized Composite Toxicity Index.")

    with st.form("calc_form"):
        col1, col2 = st.columns(2)
        with col1:
            zip_input = st.text_input("Donor Primary ZIP Code", value="02719", help="E.g., 02719 for Fairhaven/New Bedford.")
        with col2:
            radius_input = st.slider("Evaluation Radius (Miles)", min_value=1, max_value=25, value=10)

        submitted = st.form_submit_button("Calculate Exposome Risk", type="primary")

    if submitted:
        donor_point, location_data = mapper.geocode_zip(zip_input)
        
        if donor_point is None:
            st.error("Invalid ZIP code. Please try again.")
        else:
            state_abbr = location_data.state_code
            
            st.markdown("---")
            st.markdown(f"### Fetching Environmental Data for {state_abbr}")
            progress_bar = st.progress(0)
            status_placeholder = st.empty()
            
            epa_data = mapper.compile_exposome_data(donor_point, radius_input, progress_bar, status_placeholder)
            
            if epa_data.empty:
                st.error("Data pull failed or returned zero coordinate-valid sites.")
            else:
                composite_score, raw_score, contributing_sites = mapper.calculate_exposome_score(
                    donor_point, epa_data, buffer_miles=radius_input
                )
                
                status_placeholder.empty() 
                progress_bar.empty() 
                
                st.subheader(f"Results for {location_data.place_name}, {state_abbr}")
                
                res_col1, res_col2 = st.columns([1, 2])
                
                with res_col1:
                    st.metric(label="Composite Exposome Index (0-100)", value=f"{composite_score}")
                    st.caption(f"Raw IDW Sum: {raw_score}")
                    
                    if composite_score > 75:
                        st.error("Severe Environmental Risk Detected")
                    elif composite_score > 50:
                        st.warning("High Environmental Risk")
                    elif composite_score > 25:
                        st.info("Moderate Environmental Risk")
                    else:
                        st.success("Minimal Identified Risk")
                        
                with res_col2:
                    if not contributing_sites.empty:
                        contributing_sites = contributing_sites.sort_values(by='RAW_RISK_CONTRIBUTION', ascending=False)
                        st.dataframe(contributing_sites[['FACILITY_NAME', 'DATABASE', 'DISTANCE_MILES', 'RAW_RISK_CONTRIBUTION']], use_container_width=True)
                    else:
                        st.info(f"No active or historical toxic sites found within {radius_input} miles.")

                st.markdown("### Spatial Distribution Map")
                m = folium.Map(location=[donor_point.y, donor_point.x], zoom_start=11)
                
                folium.Marker(
                    [donor_point.y, donor_point.x],
                    popup="Donor Residence",
                    icon=folium.Icon(color="green", icon="home")
                ).add_to(m)
                
                folium.Circle(
                    radius=radius_input * 1609.34,
                    location=[donor_point.y, donor_point.x],
                    color="blue",
                    fill=True,
                    fill_opacity=0.1
                ).add_to(m)
                
                if not contributing_sites.empty:
                    for _, row in contributing_sites.iterrows():
                        marker_color = "darkred" if 'SEMS' in row['DATABASE'] else "orange"
                        
                        folium.CircleMarker(
                            location=[row['LATITUDE'], row['LONGITUDE']],
                            radius=6,
                            popup=f"{row['FACILITY_NAME']} ({row['DATABASE']})",
                            color=marker_color,
                            fill=True,
                            fill_color=marker_color
                        ).add_to(m)
                
                st_folium(m, width=800, height=400, returned_objects=[])

elif page == "Patient Education & Science":
    st.title("The Science Behind the Exposome")
    
    st.markdown("""
    ### What is the "Exposome"?
    When evaluating deceased organ health, surgical guidelines traditionally look at genetics, serology, and medical history. However, this misses the **Exposome**—the cumulative measure of environmental influences and associated biological responses throughout a donor's lifespan.
    
    In nephrology, kidneys continuously filter toxicants from the blood. Decades of micro-exposure to environmental heavy metals (like lead and cadmium) and volatile organic compounds (VOCs) can induce tubulointerstitial micro-damage and accelerated cellular senescence that standard blood tests (like serum creatinine) may not detect until significant physiological function is already lost.
    """)
    
    st.markdown("### Mathematical Methodology & Composite Scaling")
    st.markdown("""
    This tool utilizes **Inverse Distance Weighting (IDW)**, a deterministic spatial interpolation model heavily utilized in geospatial epidemiology. The core assumption is that the physiological risk imparted by a toxic facility decays exponentially as linear distance increases.
    
    To make this data clinically interpretable, the raw IDW sum is transformed into a **Composite Toxin Risk Index (0-100)** utilizing logarithmic normalization, mitigating extreme outliers caused by immediate geographic proximity to a facility.
    
    The Composite Exposome Index ($CI$) is calculated as:
    """)
    
    st.latex(r"CI = \min\left(100, \ln\left(1 + \sum_{i=1}^{n} \frac{W_i}{d_i^2}\right) \times C\right)")
    
    st.markdown("""
    **Where:**
    * $CI$ = Composite Exposome Index (Bounded 0-100)
    * $n$ = Number of EPA-registered toxic sites within the defined evaluation radius
    * $W_i$ = The categorical hazard multiplier (Superfund sites carry a statistically heavier historical weight than active TRI manufacturing)
    * $d_i$ = The linear distance from the donor's residence to the facility $i$ (in miles)
    * $C$ = A constant visual scaling factor to distribute average urban scores cleanly across the 0-100 range
    """)
    
    st.markdown("---")
    st.markdown("### Relevant Literature & Citations")
    st.markdown("""
    1.  **Wild, C. P. (2005).** *Complementing the genome with an "exposome": the outstanding challenge of environmental exposure measurement in molecular epidemiology.* Cancer Epidemiology, Biomarkers & Prevention, 14(8), 1847-1850.
    2.  **Weaver, V. M., et al. (2015).** *Environmental chemicals and chronic kidney disease: intersections and future directions.* Current Environmental Health Reports, 2(1), 84-93.
    3.  **United States Environmental Protection Agency.** *Risk-Screening Environmental Indicators (RSEI) Methodology.* Data and algorithms regarding categorical hazard multipliers for industrial facilities.
    """)
    
    st.info("Data for this tool is sourced from the US Environmental Protection Agency (EPA) Toxics Release Inventory (TRI) database and the Superfund Enterprise Management System (SEMS) via the spatial FRS integration.")