import math

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculates the haversine distance between two points in meters.
    """
    R = 6371000 # Radius of the earth in meters
    try:
        lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c)
    except (ValueError, TypeError):
        return 999999 # Return a large distance on error
