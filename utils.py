import re

def try_parse_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def parse_subject_title(subject):
    if not subject:
        return None
    
    # Try S01E01 style first
    match = re.match(r'^(.+?)\s*-\s*[Ss](\d+)[Ee](\d+)(?:\s*-\s*(.+))?$', subject)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3))
    
    # Try 1x01 style
    match = re.match(r'^(.+?)\s*-\s*(\d+)x(\d+)(?:\s*-\s*(.+))?$', subject)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3))
        
    return None
