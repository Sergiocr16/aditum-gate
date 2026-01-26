/**
 * Configuration Manager for Aditum Gate System (Node.js)
 * Fetches configuration from external API and manages local caching
 */

const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');

// Get the directory where config-manager.js is located
const CONFIG_MANAGER_DIR = __dirname;

const DEVICE_ID_FILE = path.join(CONFIG_MANAGER_DIR, 'device-id.txt');
const CONFIG_CACHE_FILE = path.join(CONFIG_MANAGER_DIR, 'config-cache.json');
const CONFIG_DEFAULT_FILE = path.join(CONFIG_MANAGER_DIR, 'config-default.json');

let _configCache = null;
let _pollingInterval = null;
let _lastConfigHash = null;

/**
 * Read device ID from device-id.txt file
 */
function getDeviceId() {
    try {
        if (fs.existsSync(DEVICE_ID_FILE)) {
            const deviceId = fs.readFileSync(DEVICE_ID_FILE, 'utf8').trim();
            if (deviceId) {
                return deviceId;
            }
        }
    } catch (error) {
        console.error(`Error reading device ID: ${error.message}`);
    }
    
    return 'DEVICE-001';
}

/**
 * Load default configuration from config-default.json
 */
function loadDefaultConfig() {
    try {
        if (fs.existsSync(CONFIG_DEFAULT_FILE)) {
            const content = fs.readFileSync(CONFIG_DEFAULT_FILE, 'utf8');
            return JSON.parse(content);
        }
    } catch (error) {
        console.error(`Error loading default config: ${error.message}`);
    }
    
    // Return minimal default config if file doesn't exist
    return {
        device: { deviceId: 'DEVICE-001', deviceName: 'Default Device', scannerType: 'qr', scannerScript: 'scannerQr.py' },
        hardware: { hasScreen: true, hasTwoCameras: true, isScreen: true, deviceName: 'Newtologic  4010E' },
        door: { doorType: 'entry', doorId: '0', placeName: 'Name' },
        display: { clientLogoUrl: 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg', showCameraFeed: true },
        api: { baseUrl: 'https://app.aditumcr.com/api' },
        polling: { intervalSeconds: 30, enabled: true }
    };
}

/**
 * Load configuration from local cache file
 */
function loadCachedConfig() {
    try {
        if (fs.existsSync(CONFIG_CACHE_FILE)) {
            const content = fs.readFileSync(CONFIG_CACHE_FILE, 'utf8');
            return JSON.parse(content);
        }
    } catch (error) {
        console.error(`Error loading cached config: ${error.message}`);
    }
    
    return null;
}

/**
 * Save configuration to local cache file
 */
function saveConfigCache(config) {
    try {
        fs.writeFileSync(CONFIG_CACHE_FILE, JSON.stringify(config, null, 2), 'utf8');
    } catch (error) {
        console.error(`Error saving config cache: ${error.message}`);
    }
}

/**
 * Fetch configuration from external API
 */
function fetchConfigFromApi(deviceId, baseUrl = null) {
    return new Promise((resolve, reject) => {
        if (!baseUrl) {
            const defaultConfig = loadDefaultConfig();
            baseUrl = defaultConfig.api?.baseUrl || 'https://app.aditumcr.com/api';
        }
        
        // Derive config API URL from baseUrl (remove /api if present, then add /api/devices/{id}/config)
        const urlObj = new URL(baseUrl);
        const baseDomain = `${urlObj.protocol}//${urlObj.host}`;
        const url = `${baseDomain}/api/devices/${deviceId}/config`;
        const urlObj = new URL(url);
        const isHttps = urlObj.protocol === 'https:';
        const httpModule = isHttps ? https : http;
        
        const options = {
            hostname: urlObj.hostname,
            port: urlObj.port || (isHttps ? 443 : 80),
            path: urlObj.pathname + urlObj.search,
            method: 'GET',
            timeout: 10000
        };
        
        const req = httpModule.request(options, (res) => {
            let data = '';
            
            res.on('data', (chunk) => {
                data += chunk;
            });
            
            res.on('end', () => {
                if (res.statusCode === 200) {
                    try {
                        resolve(JSON.parse(data));
                    } catch (error) {
                        reject(new Error(`Failed to parse JSON: ${error.message}`));
                    }
                } else {
                    reject(new Error(`API returned status ${res.statusCode}`));
                }
            });
        });
        
        req.on('error', (error) => {
            reject(error);
        });
        
        req.on('timeout', () => {
            req.destroy();
            reject(new Error('Request timeout'));
        });
        
        req.end();
    });
}

/**
 * Compare two configs and return list of changed field paths
 */
function compareConfigs(oldConfig, newConfig) {
    if (!oldConfig) {
        return null;
    }
    
    const changedFields = [];
    
    function compareObjects(old, newObj, currentPath = '') {
        if (typeof old !== 'object' || typeof newObj !== 'object' || old === null || newObj === null) {
            if (old !== newObj) {
                changedFields.push(currentPath || 'root');
            }
            return;
        }
        
        const allKeys = new Set([...Object.keys(old), ...Object.keys(newObj)]);
        for (const key of allKeys) {
            const path = currentPath ? `${currentPath}.${key}` : key;
            if (!(key in old)) {
                changedFields.push(path);
            } else if (!(key in newObj)) {
                changedFields.push(path);
            } else {
                if (typeof old[key] === 'object' && typeof newObj[key] === 'object' && old[key] !== null && newObj[key] !== null) {
                    compareObjects(old[key], newObj[key], path);
                } else if (old[key] !== newObj[key]) {
                    changedFields.push(path);
                }
            }
        }
    }
    
    compareObjects(oldConfig, newConfig);
    return changedFields.length > 0 ? changedFields : null;
}

/**
 * Update configuration from external API
 */
async function updateConfig() {
    const deviceId = getDeviceId();
    const defaultConfig = loadDefaultConfig();
    const baseUrl = defaultConfig.api?.baseUrl || 'https://app.aditumcr.com/api';
    
    try {
        const newConfig = await fetchConfigFromApi(deviceId, baseUrl);
        
        if (!newConfig) {
            // API unavailable, try to use cached config
            const cachedConfig = loadCachedConfig();
            if (cachedConfig) {
                console.log('Using cached config (API unavailable)');
                _configCache = cachedConfig;
                return;
            } else {
                // No cache, use default
                console.log('Using default config (API unavailable, no cache)');
                _configCache = defaultConfig;
                saveConfigCache(defaultConfig);
                return;
            }
        }
        
        // Check if config changed
        const oldConfig = _configCache;
        const configHash = JSON.stringify(newConfig);
        
        if (!_lastConfigHash || configHash !== _lastConfigHash) {
            const changedFields = compareConfigs(oldConfig, newConfig);
            
            _configCache = newConfig;
            _lastConfigHash = configHash;
            
            saveConfigCache(newConfig);
            
            if (changedFields) {
                console.log(`Configuration updated. Changed fields: ${changedFields.join(', ')}`);
            } else {
                console.log('Configuration reloaded (first load)');
            }
        } else {
            console.log('Configuration unchanged');
        }
    } catch (error) {
        console.error(`Error updating config: ${error.message}`);
        
        // Fallback to cached or default
        const cachedConfig = loadCachedConfig();
        if (cachedConfig) {
            _configCache = cachedConfig;
        } else {
            _configCache = defaultConfig;
            saveConfigCache(defaultConfig);
        }
    }
}

/**
 * Start background polling for config updates
 */
function startPolling() {
    if (_pollingInterval) {
        return; // Already polling
    }
    
    const defaultConfig = loadDefaultConfig();
    const pollingConfig = defaultConfig.polling || {};
    const interval = (pollingConfig.intervalSeconds || 30) * 1000;
    
    if (pollingConfig.enabled !== false) {
        _pollingInterval = setInterval(() => {
            updateConfig().catch(error => {
                console.error(`Error in polling: ${error.message}`);
            });
        }, interval);
        
        console.log('Configuration polling started');
    }
}

/**
 * Stop background polling
 */
function stopPolling() {
    if (_pollingInterval) {
        clearInterval(_pollingInterval);
        _pollingInterval = null;
        console.log('Configuration polling stopped');
    }
}

/**
 * Get current configuration (returns a copy)
 */
function getConfig() {
    if (!_configCache) {
        // First load - try cache, then default
        const cached = loadCachedConfig();
        if (cached) {
            _configCache = cached;
        } else {
            _configCache = loadDefaultConfig();
            saveConfigCache(_configCache);
        }
    }
    
    // Return a deep copy to prevent external modifications
    return JSON.parse(JSON.stringify(_configCache));
}

/**
 * Initialize configuration system
 */
async function initConfig() {
    await updateConfig();
    startPolling();
}

// Auto-initialize when module is loaded
if (require.main !== module) {
    initConfig().catch(error => {
        console.error(`Error initializing config: ${error.message}`);
    });
}

module.exports = {
    getConfig,
    getDeviceId,
    initConfig,
    updateConfig,
    startPolling,
    stopPolling
};