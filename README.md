# 🫘 The Donor Exposome Nephrotoxin Mapper

A clinical geospatial application designed to evaluate the cumulative environmental toxicant exposure of deceased organ donors, providing a novel metric for assessing organ longevity and potential tubulointerstitial micro-damage prior to transplantation.

## The Clinical Problem
Current organ evaluation guidelines—including the Kidney Donor Profile Index (KDPI)—rely on physiological snapshots: age, history of hypertension/diabetes, and serum creatinine. However, these metrics miss the **Exposome**—the lifetime cumulative environmental exposures of the donor. A kidney from a 30-year-old donor residing near an industrial solvent plant may possess decades of micro-damage from heavy metals and VOCs that standard serology cannot detect until significant functional loss has occurred.

## The Solution
This tool introduces a **Composite Toxicity Index (0-100)** to quantify the environmental risk side of the G x E (Genetics x Environment) equation. By inputting a donor's residential ZIP code, the application cross-references local geography against two major EPA databases to calculate an inverse-distance-weighted risk score.

### Key Features
* **Dual-Database Integration:** Natively queries both the EPA Toxics Release Inventory (Active TRIS sites) and the Facility Registry Service (Historical SEMS/Superfund sites).
* **Fault-Tolerant Spatial API:** Utilizes the EPA's FRS spatial endpoints with an integrated retry/backoff mechanism to bypass legacy government server instability (HTTP 500/503 errors).
* **Normalized Risk Scoring:** Applies logarithmic normalization to the spatial IDW sum, bounding the final clinical risk score to an intuitive 0-100 scale.
* **Interactive Mapping:** Generates real-time, interactive Folium maps visually distinguishing between active manufacturing hazards and legacy Superfund remediation sites.

## Scientific Methodology
This tool utilizes **Inverse Distance Weighting (IDW)**, a standard deterministic spatial interpolation model used in geospatial epidemiology. The core assumption is that the physiological risk imparted by a toxic facility decays exponentially as linear distance increases.

The Composite Exposome Index (CI) is calculated using categorical hazard multipliers (e.g., historical Superfund sites carry a heavier base weight than active reporting sites) and floor-limits distance to prevent division-by-zero singularities. 

`CI = min(100, ln(1 + sum(W / d^2)) * C)`
*(Where W = Hazard Weight, d = distance in miles, and C = visual scaling constant).*

## Installation & Local Usage

1. Clone the repository:
   ```bash
   git clone [https://github.com/YourUsername/donor-exposome-mapper.git](https://github.com/YourUsername/donor-exposome-mapper.git)
   cd donor-exposome-mapper
