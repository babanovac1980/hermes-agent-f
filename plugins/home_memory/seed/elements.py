# Verbatim port of SeedData/elements.json.
# parent uses long-name path (as written in the JSON); seed logic resolves via fullname map.
ELEMENTS = [
    {"name": "House",        "category": "House",        "parent": None,                 "short_name": None, "sort_index": 0},

    {"name": "Ground Floor", "category": "Floor",        "parent": "House",              "short_name": "GF", "sort_index": 0},
    {"name": "Hallway",      "category": "Room",         "parent": "House/Ground Floor", "short_name": None, "sort_index": 0},
    {"name": "Living Room",  "category": "Room",         "parent": "House/Ground Floor", "short_name": None, "sort_index": 1},
    {"name": "Kitchen",      "category": "Room",         "parent": "House/Ground Floor", "short_name": None, "sort_index": 2},
    {"name": "Bedroom",      "category": "Room",         "parent": "House/Ground Floor", "short_name": None, "sort_index": 3},
    {"name": "WC",           "category": "Room",         "parent": "House/Ground Floor", "short_name": None, "sort_index": 4},

    {"name": "Upper Floor",  "category": "Floor",        "parent": "House",              "short_name": "UF", "sort_index": 1},
    {"name": "Hallway",      "category": "Room",         "parent": "House/Upper Floor",  "short_name": None, "sort_index": 0},
    {"name": "Bedroom",      "category": "Room",         "parent": "House/Upper Floor",  "short_name": None, "sort_index": 1},
    {"name": "Bathroom",     "category": "Room",         "parent": "House/Upper Floor",  "short_name": None, "sort_index": 2},

    {"name": "Garage",       "category": "Garage",       "parent": None,                 "short_name": None, "sort_index": 1},

    {"name": "Outdoor Area", "category": "Outdoor Area", "parent": None,                 "short_name": None, "sort_index": 2},
    {"name": "Terrace",      "category": "Terrace Area", "parent": "Outdoor Area",       "short_name": None, "sort_index": 0},
    {"name": "Garden",       "category": "Garden Area",  "parent": "Outdoor Area",       "short_name": None, "sort_index": 1},
]
