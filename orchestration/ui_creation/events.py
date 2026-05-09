def make_event(event_type: str, node: str, message: str, data: dict | None = None) -> dict:
    return {
        "event_type": event_type,
        "node": node,
        "message": message,
        "data": data or {},
    }


def make_sub_event(event_type: str, parent_node: str, subnode_id: str, label: str, data: dict | None = None):
    return {
        "event_type": event_type,
        "parent_node": parent_node,
        "subnode_id": subnode_id,
        "label": label,
        "data": data or {},
    }
