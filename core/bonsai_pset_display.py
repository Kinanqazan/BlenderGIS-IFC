"""Show a real IFC property named 'id' in Bonsai's property set panel."""
import sys


_restore = None


def install():
    """Wait for Bonsai to initialize before adapting its panel data."""
    global _restore
    if _restore is not None:
        return None

    module = sys.modules.get("bonsai.bim.module.pset.data")
    if module is None:
        return 2.0
    data_class = getattr(module, "Data", None)
    original = vars(data_class).get("psetqtos") if data_class is not None else None
    if not isinstance(original, classmethod) or not hasattr(data_class, "property_display_data"):
        return None

    from ifcopenshell.util.element import get_property

    @classmethod
    def psetqtos(cls, element, *args, **kwargs):
        rows = original.__func__(cls, element, *args, **kwargs)
        for row in rows:
            if any(prop["Name"] == "id" for prop in row["Properties"]):
                continue
            pset = element.file.by_id(row["id"])
            if pset.is_a("IfcPropertySet"):
                prop = get_property(pset.HasProperties, "id", verbose=True)
                if prop is not None:
                    row["Properties"].append(cls.property_display_data(element.file, "id", prop))
                    row["Properties"].sort(key=lambda item: item["Name"])
        return rows

    data_class.psetqtos = psetqtos
    module.refresh()

    def restore():
        if data_class.__dict__.get("psetqtos") is psetqtos:
            data_class.psetqtos = original
            module.refresh()

    _restore = restore


def uninstall():
    global _restore
    if _restore is not None:
        _restore()
        _restore = None
