/**
 * Monad Validator Monitor - Dashboard Application
 * Tenderduty-style minimalist design with Monad brand colors
 * Real-time validator status monitoring with per-validator Huginn uptime
 */

(function() {
    'use strict';

    // Configuration
    const CONFIG = {
        healthEndpoint: '/health',
        pollingInterval: 5000, // 5 seconds
        reconnectDelay: 3000,
        maxReconnectAttempts: 10
    };

    // State - per-validator tracking
    let state = {
        isConnected: false,
        reconnectAttempts: 0,
        lastUpdate: null,
        pollTimer: null,
        validators: {}  // Per-validator state tracking
    };

    // DOM Elements
    const elements = {
        validatorsContainer: document.getElementById('validators-container'),
        connectionDot: document.getElementById('connection-dot'),
        connectionStatus: document.getElementById('connection-status'),
        refreshInfo: document.getElementById('refresh-info'),
        cardTemplate: document.getElementById('validator-card-template'),
        networkBadge: document.getElementById('network-badge')
    };

    /**
     * Format number with thousand separators
     * @param {number} num - Number to format
     * @returns {string} Formatted number
     */
    function formatNumber(num) {
        if (num === null || num === undefined) {
            return 'N/A';
        }
        return num.toLocaleString('en-US');
    }

    /**
     * Format uptime percentage
     * @param {number} percent - Uptime percentage
     * @returns {string} Formatted percentage
     */
    function formatUptimePercent(percent) {
        if (percent === null || percent === undefined) {
            return 'N/A';
        }
        return percent.toFixed(2) + '%';
    }

    /**
     * Format time ago from timestamp
     * @param {number} timestamp - Unix timestamp in seconds
     * @returns {string} Human readable time ago
     */
    function formatTimeAgo(timestamp) {
        if (!timestamp) {
            return 'N/A';
        }

        const now = Date.now() / 1000;
        const diff = Math.floor(now - timestamp);

        if (diff < 0) {
            return 'Just now';
        }
        if (diff < 60) {
            return `${diff}s ago`;
        } else if (diff < 3600) {
            const minutes = Math.floor(diff / 60);
            return `${minutes}m ago`;
        } else {
            const hours = Math.floor(diff / 3600);
            return `${hours}h ago`;
        }
    }

    /**
     * Get status class based on validator state
     * @param {string} state - Validator state
     * @param {boolean} healthy - Is validator healthy
     * @param {number} fails - Number of failures
     * @returns {string} Status class name
     */
    function getStatusClass(state, healthy, fails) {
        if (!healthy || fails > 0) {
            return 'critical';
        }
        if (state === 'active') {
            return 'active';
        }
        if (state === 'warning') {
            return 'warning';
        }
        return 'inactive';
    }

    /**
     * Get status display text
     * @param {string} state - Validator state
     * @param {boolean} healthy - Is validator healthy
     * @param {number} fails - Number of failures
     * @returns {string} Status display text
     */
    function getStatusText(state, healthy, fails) {
        if (!healthy || fails > 0) {
            return 'CRITICAL';
        }
        if (state === 'active') {
            return 'ACTIVE';
        }
        if (state === 'warning') {
            return 'WARNING';
        }
        return 'INACTIVE';
    }

    /**
     * Get network display name
     * @param {string} network - Network identifier
     * @returns {string} Human readable network name
     */
    function getNetworkDisplayName(network) {
        if (!network) return 'Unknown';
        return network.charAt(0).toUpperCase() + network.slice(1);
    }

    /**
     * Create validator card element
     * @param {string} name - Validator name
     * @param {Object} data - Validator data
     * @returns {HTMLElement} Card element
     */
    function createValidatorCard(name, data) {
        const template = elements.cardTemplate;
        const card = template.content.cloneNode(true).querySelector('.validator-card');

        card.dataset.validatorName = name;

        const validatorState = data.state || 'unknown';
        const healthy = data.healthy !== false;
        const fails = data.fails || 0;

        const statusClass = getStatusClass(validatorState, healthy, fails);
        const statusText = getStatusText(validatorState, healthy, fails);

        // Set validator name
        card.querySelector('.validator-name').textContent = name;

        // Set status indicator
        const indicator = card.querySelector('.validator-indicator');
        indicator.className = `validator-indicator ${statusClass}`;

        // Set status badge
        const badge = card.querySelector('.validator-status-badge');
        badge.className = `validator-status-badge ${statusClass}`;
        badge.textContent = statusText;

        // Set metrics
        card.querySelector('.metric-height').textContent = formatNumber(data.height);
        card.querySelector('.metric-peers').textContent = formatNumber(data.peers);

        // Set Huginn uptime percentage if available
        const uptimeElement = card.querySelector('.metric-uptime');
        if (data.huginn_data && data.huginn_data.uptime_percent !== undefined) {
            uptimeElement.textContent = formatUptimePercent(data.huginn_data.uptime_percent);
            uptimeElement.title = `Finalized: ${data.huginn_data.finalized_count || 0} / Timeouts: ${data.huginn_data.timeout_count || 0}`;
        } else {
            uptimeElement.textContent = 'N/A';
            uptimeElement.title = 'Huginn data not available';
        }

        // Set fails with conditional styling
        const failsElement = card.querySelector('.metric-fails');
        failsElement.textContent = fails;
        if (fails > 0) {
            failsElement.classList.add('has-fails');
        } else {
            failsElement.classList.remove('has-fails');
        }

        // Set last check time from per-validator timestamp
        const lastCheckElement = card.querySelector('.last-check-time');
        if (data.last_check) {
            lastCheckElement.textContent = formatTimeAgo(data.last_check);
            // Store timestamp for updates
            card.dataset.lastCheck = data.last_check;
        } else {
            lastCheckElement.textContent = 'Just now';
        }

        return card;
    }

    /**
     * Update existing validator card
     * @param {HTMLElement} card - Card element
     * @param {Object} data - Validator data
     */
    function updateValidatorCard(card, data) {
        const validatorState = data.state || 'unknown';
        const healthy = data.healthy !== false;
        const fails = data.fails || 0;

        const statusClass = getStatusClass(validatorState, healthy, fails);
        const statusText = getStatusText(validatorState, healthy, fails);

        // Update status indicator
        const indicator = card.querySelector('.validator-indicator');
        indicator.className = `validator-indicator ${statusClass}`;

        // Update status badge
        const badge = card.querySelector('.validator-status-badge');
        badge.className = `validator-status-badge ${statusClass}`;
        badge.textContent = statusText;

        // Update metrics
        card.querySelector('.metric-height').textContent = formatNumber(data.height);
        card.querySelector('.metric-peers').textContent = formatNumber(data.peers);

        // Update Huginn uptime percentage
        const uptimeElement = card.querySelector('.metric-uptime');
        if (data.huginn_data && data.huginn_data.uptime_percent !== undefined) {
            uptimeElement.textContent = formatUptimePercent(data.huginn_data.uptime_percent);
            uptimeElement.title = `Finalized: ${data.huginn_data.finalized_count || 0} / Timeouts: ${data.huginn_data.timeout_count || 0}`;
        } else {
            uptimeElement.textContent = 'N/A';
            uptimeElement.title = 'Huginn data not available';
        }

        // Update fails with conditional styling
        const failsElement = card.querySelector('.metric-fails');
        failsElement.textContent = fails;
        if (fails > 0) {
            failsElement.classList.add('has-fails');
        } else {
            failsElement.classList.remove('has-fails');
        }

        // Update last check time from per-validator timestamp
        if (data.last_check) {
            card.dataset.lastCheck = data.last_check;
        }

        // Remove error state if present
        card.classList.remove('error');
    }

    /**
     * Update connection status UI
     * @param {boolean} connected - Connection status
     */
    function updateConnectionStatus(connected) {
        state.isConnected = connected;

        if (connected) {
            elements.connectionDot.className = 'connection-dot connected';
            elements.connectionStatus.textContent = 'Connected';
            state.reconnectAttempts = 0;
        } else {
            elements.connectionDot.className = 'connection-dot disconnected';
            elements.connectionStatus.textContent = 'Disconnected';
        }
    }

    /**
     * Update network badge
     * @param {string} network - Network name
     */
    function updateNetworkBadge(network) {
        if (elements.networkBadge) {
            elements.networkBadge.textContent = getNetworkDisplayName(network);
            elements.networkBadge.className = `network-badge ${network || 'unknown'}`;
        }
    }

    /**
     * Render validators from health data
     * @param {Object} data - Health endpoint response
     */
    function renderValidators(data) {
        const validators = data.validators || {};
        const existingCards = new Map();

        // Collect existing cards
        elements.validatorsContainer.querySelectorAll('.validator-card').forEach(card => {
            existingCards.set(card.dataset.validatorName, card);
        });

        // Update or create cards
        Object.entries(validators).forEach(([name, validatorData]) => {
            const existingCard = existingCards.get(name);

            // Store per-validator state
            state.validators[name] = {
                lastCheck: validatorData.last_check,
                huginnData: validatorData.huginn_data
            };

            if (existingCard) {
                updateValidatorCard(existingCard, validatorData);
                existingCards.delete(name);
            } else {
                const newCard = createValidatorCard(name, validatorData);
                elements.validatorsContainer.appendChild(newCard);
            }
        });

        // Update network badge
        updateNetworkBadge(data.network);

        // Remove cards for validators no longer in response
        existingCards.forEach((card) => {
            card.remove();
        });

        // Add class for single validator layout
        const validatorCount = Object.keys(validators).length;
        if (validatorCount === 1) {
            elements.validatorsContainer.classList.add('has-single-validator');
        } else {
            elements.validatorsContainer.classList.remove('has-single-validator');
        }

        state.lastUpdate = Date.now();
    }

    /**
     * Handle fetch error
     * @param {Error} error - Error object
     */
    function handleError(error) {
        console.error('Health check failed:', error);
        updateConnectionStatus(false);

        // Mark all cards as having connection issues
        elements.validatorsContainer.querySelectorAll('.validator-card').forEach(card => {
            card.classList.add('error');
            card.querySelector('.last-check-time').textContent = 'Connection failed';
        });

        // Attempt reconnection
        if (state.reconnectAttempts < CONFIG.maxReconnectAttempts) {
            state.reconnectAttempts++;
            console.log(`Reconnection attempt ${state.reconnectAttempts}/${CONFIG.maxReconnectAttempts}`);
        }
    }

    /**
     * Fetch health data from endpoint
     */
    async function fetchHealth() {
        try {
            const response = await fetch(CONFIG.healthEndpoint, {
                method: 'GET',
                headers: {
                    'Accept': 'application/json'
                }
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();
            updateConnectionStatus(true);
            renderValidators(data);

        } catch (error) {
            handleError(error);
        }
    }

    /**
     * Update "last check" times for all cards based on per-validator timestamps
     */
    function updateLastCheckTimes() {
        elements.validatorsContainer.querySelectorAll('.validator-card').forEach(card => {
            const name = card.dataset.validatorName;
            const validatorState = state.validators[name];

            if (validatorState && validatorState.lastCheck) {
                const lastCheckElement = card.querySelector('.last-check-time');
                if (lastCheckElement && lastCheckElement.textContent !== 'Connection failed') {
                    lastCheckElement.textContent = formatTimeAgo(validatorState.lastCheck);
                }
            }
        });
    }

    /**
     * Start polling for health updates
     */
    function startPolling() {
        // Initial fetch
        fetchHealth();

        // Set up polling interval
        state.pollTimer = setInterval(() => {
            fetchHealth();
            updateLastCheckTimes();
        }, CONFIG.pollingInterval);
    }

    /**
     * Stop polling
     */
    function stopPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    /**
     * Initialize the dashboard
     */
    function init() {
        console.log('Monad Validator Monitor Dashboard initializing...');

        // Check for required elements
        if (!elements.validatorsContainer || !elements.cardTemplate) {
            console.error('Required DOM elements not found');
            return;
        }

        // Start polling
        startPolling();

        // Handle visibility change (pause when tab is hidden)
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopPolling();
            } else {
                startPolling();
            }
        });

        console.log('Dashboard initialized successfully');
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
