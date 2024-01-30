#!/usr/bin/env python3
import shutil

# Method for handeling error logs.
def size_of_format(num, suffix='B'):
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f} Y{suffix}"

def parse_size_unit(input_str):
    try:
        # Split the input into size and unit
        size_str, unit = input_str[:-1], input_str[-1]
        # Convert size to an integer
        size = int(size_str)
        return size, unit
    except ValueError:
        raise ValueError("Invalid size/unit format. Example format: '500G'")

def convert_size_to_bytes(size, unit):
    units = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
    if unit in units:
        return size * units[unit]
    else:
        raise ValueError(f"Invalid unit: {unit}")
    
def valid_unit(self, unit):
    valid_units = ('K', 'M', 'G', 'T')
    if unit not in valid_units:
        raise ValueError(f"Invalid unit: {unit}. Valid units are {', '.join(valid_units)}")
    return True

def is_in_fstab(mount_point):
    with open('/etc/fstab', 'r') as fstab:
        return any(mount_point in line for line in fstab)
    
def get_available_space(path, unit=None):
    try:
        # Get disk usage statistics for the specified path
        usage = shutil.disk_usage(path)

        # Calculate the available space in bytes
        available_space = usage.free

        # Convert available space to a human-readable format (e.g., GiB)
        if unit:
            units = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
            if unit in units:
                return available_space * units[unit]

        return available_space

    except Exception as e:
        # Handle any errors that may occur
        raise ValueError(f"Error retrieving disk usage: {e}")
    