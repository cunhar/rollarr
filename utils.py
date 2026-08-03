import re

def try_parse_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def parse_episodes(text):
    if not text:
        return []
    
    # List of patterns to search for.
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
    
    results = []
    # Split text into lines to process each line individually
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                show = match.group(1).strip()
                # Clean up leading emojis, checkmarks, bullets, quotes or non-alphanumeric prefix garbage
                show = re.sub(r'^[^a-zA-Z0-9\s\'\"]+', '', show).strip()
                # Strip remaining edge quotes
                show = show.strip("'\"").strip()
                results.append((show, int(match.group(2)), int(match.group(3))))
                break
    return results

def parse_subject_title(text):
    eps = parse_episodes(text)
    return eps[0] if eps else None

