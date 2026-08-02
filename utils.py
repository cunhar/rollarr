import re

def try_parse_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def parse_subject_title(text):
    if not text:
        return None
    
    # List of patterns to search for inside the text.
    # We prioritize patterns with quotes to isolate the title block cleanly.
    patterns = [
        # With quotes: 'Futurama - season 8 - episode 1'
        r"['\"](.+?)\s*-\s*season\s+(\d+)\s*-\s*episode\s+(\d+)['\"]",
        # Without quotes: Futurama - season 8 - episode 1
        r"(.+?)\s*-\s*season\s+(\d+)\s*-\s*episode\s+(\d+)",
        # With quotes: 'Futurama - S08E01'
        r"['\"](.+?)\s*-\s*[Ss](\d+)[Ee](\d+)['\"]",
        # Without quotes: Futurama - S08E01
        r"(.+?)\s*-\s*[Ss](\d+)[Ee](\d+)",
        # With quotes: 'Futurama - 8x01'
        r"['\"](.+?)\s*-\s*(\d+)x(\d+)['\"]",
        # Without quotes: Futurama - 8x01
        r"(.+?)\s*-\s*(\d+)x(\d+)"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            show = match.group(1).strip()
            # Clean up leading emojis, checkmarks, quotes or non-alphanumeric prefix garbage
            show = re.sub(r'^[^a-zA-Z0-9\s\'\"]+', '', show).strip()
            # Strip remaining edge quotes
            show = show.strip("'\"").strip()
            return show, int(match.group(2)), int(match.group(3))
            
    return None
