# Shapefile Import with Automatic IFC Assignment

## Overview
This feature adds automatic IFC class assignment, Property Set (Pset) mapping, and random coloring to the shapefile import process, streamlining the workflow for converting GIS data to IFC elements using Bonsai (formerly BlenderBIM).

## How to Use

1. **Import Shapefile with IFC Assignment**
   - Go to `View3D > GIS2BIM > Import > Shapefile(s) (.shp)`
   - Select one or more shapefiles
   - In the import dialog, enable **"Create IFC elements"**

2. **Configure IFC Properties**
   - **IFC Class**: Enter the IFC class name (e.g., `IfcBuilding`, `IfcSite`, `IfcBuildingElementProxy`)
   - **IFC Predefined Type** (optional): Enter the predefined type if applicable
   - **IFC User Defined Type** (optional): Enter a user-defined type if needed

3. **Random Coloring by Field (Optional)**
   - Check the **"Random color by field"** checkbox to enable random coloring
   - Select a field from the **"Color field"** dropdown to base coloring on
   - Each value in the selected field receives a deterministic color
   - Colors are applied as Blender materials for visual feedback
   - If Bonsai is active with IFC assignment, colors are also applied as IfcSurfaceStyles

4. **Complete Import**
   - Configure other import options as needed (CRS, elevation, extrusion, etc.)
   - Enable **Separate Objects** for individual object coloring (recommended for random coloring)
   - Click OK to import
   - The imported objects will automatically have IFC classes, geometry representations, and colors assigned

## Common IFC Classes for GIS Data

- `IfcSite` - For site boundaries and land parcels
- `IfcBuilding` - For building footprints
- `IfcBuildingElementProxy` - Generic building elements (default)
- `IfcRoad` - For road networks
- `IfcRailway` - For railway networks
- `IfcWaterCourse` - For water bodies
- `IfcGeographicElement` - For general geographic features

## Shapefile Field to Property Set Mapping

When importing with **"Separate Objects"** enabled, all shapefile attribute fields are automatically mapped to an IFC Property Set on each imported object. Set **Property set name** in the import options to choose its name; the default is `Pset_GIS_Attributes` (also used when the field is blank).

### Example
If your shapefile has fields like:
- `id` = 42
- `layer` = "Buildings"
- `area` = 1250.5

Each imported IFC element will contain a Pset `Pset_GIS_Attributes` with these properties:
- `id` = 42 (numeric)
- `layer` = "Buildings" (text)
- `area` = 1250.5 (numeric)

IFC property names match the shapefile fields exactly. **Object name from field** uses the selected field's value to name the object. With that option off, the object uses the shapefile name. GIS2BIM displays a lowercase `id` in Bonsai's property set panel.

**Note**: Property Sets are only created when using **Separate Objects** mode. Merged/single-object imports do not have individual feature attributes.

## Random Coloring by Field Values

The random coloring feature allows you to visually distinguish objects based on unique values in a selected field.

### How It Works
- When enabled, each value in the selected field receives a deterministic color
- Colors are generated deterministically; the same value receives the same color across imported files
- Blender materials are always applied for immediate visual feedback
- If Bonsai is active with IFC assignment, IfcSurfaceStyles are also created for proper IFC export

### Example Use Case
If you have a building shapefile with a `building_type` field containing values like "Residential", "Commercial", "Industrial":
1. Enable "Random color by field"
2. Select "building_type" as the color field
3. All residential buildings will have one color, commercial another, industrial a third
4. Colors will be consistent across all features with the same field value

### Requirements
- **Separate Objects mode**: Random coloring requires separate objects to apply individual colors
- **Field selection**: The chosen field must exist in the shapefile's attribute table
- **Bonsai/IfcOpenShell**: IfcSurfaceStyles are only applied when Bonsai is active with an open IFC project

## Integration with Bonsai/BlenderBIM

The feature directly integrates with Bonsai's API to:
1. Create proper IFC elements with the specified class
2. Export Blender mesh geometry as IFC tessellation representation
3. Create Property Sets from shapefile attributes
4. Rename objects to show the IFC class in the navigation panel (e.g., `IfcBuildingElementProxy/ObjectName`)

### Requirements
- Bonsai (BlenderBIM) must be installed and enabled
- An active IFC project must be open in Bonsai

## Example Workflow

1. Open or create an IFC project in Bonsai (`File > New IFC Project`)
2. Choose a projected metre-based IFC target CRS and confirm any requested source CRS verification or conversion.
3. Import a building footprint shapefile with:
   - IFC Class: `IfcBuilding`
   - Separate Objects: **enabled**
4. Import a site boundary shapefile with `IfcSite` as the IFC Class
5. Each building object will have:
   - Correct IFC class in the navigation panel
   - IFC geometry representation
   - `Pset_GIS_Attributes` with all shapefile fields
6. In Bonsai, use the BIM tools to:
   - Assign spatial relationships (e.g., assign buildings to the site)
   - Add additional property sets
   - Export to IFC

## Technical Details

The IFC assignment is implemented in `operators/io_import_shp.py` using:
- Bonsai's `bim.assign_class` operator for proper IFC element creation and geometry export
- `ifcopenshell.api.pset.add_pset` and `edit_pset` for Property Set creation
- Both single-object and separate-object import modes support IFC class assignment
- Property Sets are only created in **Separate Objects** mode where individual feature records are available

## Georeferencing and placement

The georeferenced IFC workflow requires an active IFC4 project and a `.prj` file for each shapefile. PROJ `projinfo` inspects source CRS definitions; coordinate conversion currently uses QGIS Python with pyproj on Windows. Verify uncertain source matches in QGIS and confirm conversions in the import dialog. The IFC target must be projected and use metres.

Choose **IFC map conversion** to store the projected CRS and offset in the IFC, or **Direct Site placement** to put projected coordinates in the Site placement without an IFC map conversion or projected CRS declaration. The choice and origin persist for subsequent imports in the same Blender scene. For Direct Site placement, save and reopen the Blender scene as well as the IFC; reopening only the IFC in a fresh scene does not restore the placement state. Use a fresh project to change modes. Save through Bonsai and verify a known point in the exported IFC. Vertical datums are not transformed. For GIS-only use, disable **Create IFC elements**.
