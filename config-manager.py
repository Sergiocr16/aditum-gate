#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration Manager for Aditum Gate System
Fetches configuration from external API and manages local caching
"""

import json
import os
import time
import threading
import requests
from urllib.parse import urlparse

# File paths
DEVICE_ID_FILE = 'device-id.txt'
CONFIG_CACHE_FILE = 'config-cache.json'
CONFIG_DEFAULT_FILE = 'config-default.json'

# Global config cache
_config_cache = None
_config_lock = threading.Lock()
_last_config_hash = None
_polling_thread = None
_polling_active = False


def get_device_id():
    """Read device ID from device-id.txt file"""
    try:
        if os.path.exists(DEVICE_ID_FILE):
            with open(DEVICE_ID_FILE, 'r') as f:
                device_id = f.read().strip()
                if device_id:
                    return device_id
    except Exception as e:
        print(f"Error reading device ID: {e}")
    
    # Return default if file doesn't exist or is empty
    return "DEVICE-001"


def load_default_config():
    """Load default configuration from config-default.json"""
    try:
        if os.path.exists(CONFIG_DEFAULT_FILE):
            with open(CONFIG_DEFAULT_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading default config: {e}")
    
    # Return minimal default config if file doesn't exist
    return {
        "device": {"deviceId": "DEVICE-001", "deviceName": "Default Device", "scannerType": "qr", "scannerScript": "scannerQr.py"},
        "hardware": {"hasScreen": True, "hasTwoCameras": True, "isScreen": True, "deviceName": "Newtologic  4010E"},
        "door": {"doorType": "entry", "doorId": "0", "placeName": "Name"},
        "display": {"clientLogoUrl": "https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg", "showCameraFeed": True},
        "api": {"baseUrl": "https://app.aditumcr.com/api"},
        "polling": {"intervalSeconds": 30, "enabled": True}
    }


def load_cached_config():
    """Load configuration from local cache file"""
    try:
        if os.path.exists(CONFIG_CACHE_FILE):
            with open(CONFIG_CACHE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading cached config: {e}")
    
    return None


def save_config_cache(config):
    """Save configuration to local cache file"""
    try:
        with open(CONFIG_CACHE_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving config cache: {e}")


def fetch_config_from_api(device_id, base_url=None):
    """Fetch configuration from external API"""
    if not base_url:
        # Try to get from default config first
        default_config = load_default_config()
        base_url = default_config.get('api', {}).get('baseUrl', 'https://app.aditumcr.com/api')
    
    # Derive config API URL from baseUrl (remove /api if present, then add /api/devices/{id}/config)
    parsed = urlparse(base_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"
    url = f"{base_domain}/api/devices/{device_id}/config"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API returned status {response.status_code} for config fetch")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching config from API: {e}")
        return None


def compare_configs(old_config, new_config):
    """Compare two configs and return list of changed field paths"""
    if old_config is None:
        return None
    
    changed_fields = []
    
    def compare_dicts(old, new, path=""):
        if not isinstance(old, dict) or not isinstance(new, dict):
            if old != new:
                changed_fields.append(path if path else "root")
            return
        
        all_keys = set(old.keys()) | set(new.keys())
        for key in all_keys:
            current_path = f"{path}.{key}" if path else key
            if key not in old:
                changed_fields.append(current_path)
            elif key not in new:
                changed_fields.append(current_path)
            else:
                if isinstance(old[key], dict) and isinstance(new[key], dict):
                    compare_dicts(old[key], new[key], current_path)
                elif old[key] != new[key]:
                    changed_fields.append(current_path)
    
    compare_dicts(old_config, new_config)
    return changed_fields if changed_fields else None


def update_config():
    """Fetch and update configuration from external API"""
    global _config_cache, _last_config_hash
    
    device_id = get_device_id()
    default_config = load_default_config()
    base_url = default_config.get('api', {}).get('baseUrl', 'https://app.aditumcr.com/api')
    
    # Try to fetch from API
    new_config = fetch_config_from_api(device_id, base_url)
    
    if new_config is None:
        # API unavailable, try to use cached config
        cached_config = load_cached_config()
        if cached_config:
            print("Using cached config (API unavailable)")
            with _config_lock:
                _config_cache = cached_config
            return
        else:
            # No cache, use default
            print("Using default config (API unavailable, no cache)")
            with _config_lock:
                _config_cache = default_config
            save_config_cache(default_config)
            return
    
    # Check if config changed
    old_config = _config_cache
    config_hash = hash(json.dumps(new_config, sort_keys=True))
    
    if _last_config_hash is None or config_hash != _last_config_hash:
        changed_fields = compare_configs(old_config, new_config)
        
        with _config_lock:
            _config_cache = new_config
            _last_config_hash = config_hash
        
        save_config_cache(new_config)
        
        if changed_fields:
            print(f"Configuration updated. Changed fields: {', '.join(changed_fields)}")
        else:
            print("Configuration reloaded (first load)")
    else:
        print("Configuration unchanged")


def polling_worker():
    """Background worker that polls for config updates"""
    global _polling_active
    
    default_config = load_default_config()
    polling_config = default_config.get('polling', {})
    interval = polling_config.get('intervalSeconds', 30)
    
    while _polling_active:
        try:
            if polling_config.get('enabled', True):
                update_config()
        except Exception as e:
            print(f"Error in polling worker: {e}")
        
        time.sleep(interval)


def start_polling():
    """Start background polling thread"""
    global _polling_thread, _polling_active
    
    if _polling_thread is None or not _polling_thread.is_alive():
        _polling_active = True
        _polling_thread = threading.Thread(target=polling_worker, daemon=True)
        _polling_thread.start()
        print("Configuration polling started")


def stop_polling():
    """Stop background polling thread"""
    global _polling_active
    
    _polling_active = False
    if _polling_thread:
        _polling_thread.join(timeout=5)
    print("Configuration polling stopped")


def get_config():
    """Get current configuration (thread-safe)"""
    global _config_cache
    
    with _config_lock:
        if _config_cache is None:
            # First load - try cache, then default
            cached = load_cached_config()
            if cached:
                _config_cache = cached
            else:
                _config_cache = load_default_config()
                save_config_cache(_config_cache)
        
        # Return a copy to prevent external modifications
        return json.loads(json.dumps(_config_cache))


def init_config():
    """Initialize configuration system - fetch from API and start polling"""
    update_config()
    start_polling()


# Initialize on import
if __name__ != "__main__":
    # Auto-initialize when imported
    init_config()