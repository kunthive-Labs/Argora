"""
sectors.py — business-type presets.

Each sector defines:
  query     : the Google Maps search term
  exclude   : category/name keywords that mean "wrong kind of business"
              (drops noise the search term drags in — e.g. a "real estate"
              search pulling in software firms)
  min_reviews: default review floor for a lead to be worth pitching

This file is just a dict. Add a sector by copying a block and editing it.
"""

SECTORS = {
    "driving-school": {
        "query": "driving school",
        "exclude": ["rto", "insurance", "car wash", "spare"],
        "min_reviews": 10,
    },
    "preschool": {
        "query": "preschool",
        "exclude": ["k12", "high school", "college", "tuition", "coaching"],
        "min_reviews": 10,
    },
    "k12-school": {
        "query": "school",
        "exclude": ["driving", "music", "dance", "gym", "coaching",
                    "college", "art ", "language", "preschool", "play school"],
        "min_reviews": 15,
    },
    "college": {
        "query": "college",
        "exclude": ["school", "coaching", "tuition", "preschool"],
        "min_reviews": 15,
    },
    "real-estate": {
        "query": "real estate agent",
        "exclude": ["software", "developer (it)", "coworking", "hardware",
                    "interior design software"],
        "min_reviews": 5,
    },
    "construction": {
        "query": "construction company",
        "exclude": ["software", "hardware store", "material supplier"],
        "min_reviews": 5,
    },
    "gym": {
        "query": "gym",
        "exclude": ["equipment", "wholesale", "supplement store"],
        "min_reviews": 15,
    },
    "dentist": {
        "query": "dental clinic",
        "exclude": ["lab", "supplier", "equipment"],
        "min_reviews": 15,
    },
    "salon": {
        "query": "beauty salon",
        "exclude": ["supplier", "wholesale", "academy"],
        "min_reviews": 15,
    },
    "coaching": {
        "query": "coaching center",
        "exclude": ["driving", "preschool", "gym"],
        "min_reviews": 10,
    },
    "clinic": {
        "query": "clinic",
        "exclude": ["dental", "veterinary", "diagnostic lab"],
        "min_reviews": 15,
    },
    "law-firm": {
        "query": "law firm",
        "exclude": ["college", "university", "notary public", "legal software",
                    "recruitment", "stationery", "books"],
        "min_reviews": 5,
    },
    "advocate": {
        "query": "advocate",
        "exclude": ["college", "institute", "coaching", "real estate",
                    "law firm software"],
        "min_reviews": 3,
    },
    "photo-studio": {
        "query": "photography studio",
        "exclude": ["camera store", "equipment", "rental", "frame shop",
                    "mobile", "printing press", "xerox"],
        "min_reviews": 5,
    },
    "photographer": {
        "query": "photographer",
        "exclude": ["camera store", "equipment", "rental", "drone shop",
                    "academy", "institute"],
        "min_reviews": 3,
    },
}


def get(name):
    """Return a sector preset, or raise with the list of valid names."""
    if name not in SECTORS:
        raise KeyError(
            f"Unknown sector '{name}'. Available: {', '.join(sorted(SECTORS))}"
        )
    return SECTORS[name]
