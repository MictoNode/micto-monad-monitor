/**
 * Monad Validator Monitor - Dashboard Application
 * Rich interactivity, animations, sparklines, and real-time feel.
 * Pure vanilla JS - no external dependencies.
 */

(function() {
    'use strict';

    // ---------------------------------------------------------------------------
    // Constants
    // ---------------------------------------------------------------------------

    const HEALTH_ENDPOINT = '/health';
    const POLLING_INTERVAL_MS = 5000;
    const POLLING_INTERVAL_SEC = 5;
    const RECONNECT_DELAY_MS = 3000;
    const MAX_RECONNECT_ATTEMPTS = 10;
    const ANIMATION_DURATION_MS = 300;
    const FLASH_DURATION_MS = 300;
    const HEIGHT_HISTORY_SIZE = 20;
    const FAIL_THRESHOLD = 3;
    const GAUGE_WARNING_MIN = 70;
    const GAUGE_CRITICAL_MIN = 90;
    const GAUGE_MAX = 100;
    const CARD_STAGGER_DELAY_SEC = 0.08;
    const SECONDS_PER_MINUTE = 60;
    const SECONDS_PER_HOUR = 3600;
    const SECONDS_PER_DAY = 86400;

    // ---------------------------------------------------------------------------
    // State
    // ---------------------------------------------------------------------------

    let state = {
        isConnected: false,
        reconnectAttempts: 0,
        lastUpdate: null,
        pollTimer: null,
        validators: {},
        previousData: {},
        heightHistory: {},
    };

    let sparklineIdCounter = 0;

    // ---------------------------------------------------------------------------
    // DOM References
    // ---------------------------------------------------------------------------

    const elements = {
        validatorsContainer: document.getElementById('validators-container'),
        connectionDot: document.getElementById('connection-dot'),
        connectionStatus: document.getElementById('connection-status'),
        cardTemplate: document.getElementById('validator-card-template'),
        totalValidators: document.getElementById('total-validators'),
        activeCount: document.getElementById('active-count'),
        warningCount: document.getElementById('warning-count'),
        criticalCount: document.getElementById('critical-count'),
        monitorUptime: document.getElementById('monitor-uptime'),
    };

    // ---------------------------------------------------------------------------
    // Formatting Helpers
    // ---------------------------------------------------------------------------

    /**
     * Format number with thousand separators.
     * @param {number} num
     * @returns {string}
     */
    function formatNumber(num) {
        if (num === null || num === undefined || isNaN(num)) {
            return 'N/A';
        }
        return num.toLocaleString('en-US');
    }

    /**
     * Format uptime percentage to two decimal places.
     * @param {number} percent
     * @returns {string}
     */
    function formatUptimePercent(percent) {
        if (percent === null || percent === undefined || isNaN(percent)) {
            return 'N/A';
        }
        return percent.toFixed(2) + '%';
    }

    /**
     * Format a TPS value with one decimal and thousand separators.
     * @param {number} tps
     * @returns {string}
     */
    function formatTps(tps) {
        if (tps === null || tps === undefined || isNaN(tps)) {
            return '--';
        }
        var parts = tps.toFixed(1).split('.');
        parts[0] = parseInt(parts[0], 10).toLocaleString('en-US');
        return parts.join('.');
    }

    /**
     * Format a percentage with one decimal.
     * @param {number} pct
     * @returns {string}
     */
    function formatPercent(pct) {
        if (pct === null || pct === undefined || isNaN(pct)) {
            return '--';
        }
        return pct.toFixed(1) + '%';
    }

    /**
     * Format monitor uptime from seconds to human-readable string.
     * Examples: "45s", "12m", "2h 15m", "3d 5h"
     * @param {number} seconds
     * @returns {string}
     */
    function formatUptime(seconds) {
        if (seconds === null || seconds === undefined || isNaN(seconds)) {
            return '--';
        }
        var sec = Math.floor(seconds);
        if (sec < SECONDS_PER_MINUTE) {
            return sec + 's';
        }
        if (sec < SECONDS_PER_HOUR) {
            return Math.floor(sec / SECONDS_PER_MINUTE) + 'm';
        }
        if (sec < SECONDS_PER_DAY) {
            var hours = Math.floor(sec / SECONDS_PER_HOUR);
            var minutes = Math.floor((sec % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
            return minutes > 0 ? hours + 'h ' + minutes + 'm' : hours + 'h';
        }
        var days = Math.floor(sec / SECONDS_PER_DAY);
        var remainingHours = Math.floor((sec % SECONDS_PER_DAY) / SECONDS_PER_HOUR);
        return remainingHours > 0 ? days + 'd ' + remainingHours + 'h' : days + 'd';
    }

    // ---------------------------------------------------------------------------
    /**
     * Format time ago from unix timestamp.
     * @param {number} timestamp
     * @returns {string}
     */
    function formatTimeAgo(timestamp) {
        if (!timestamp) return 'N/A';
        var diff = Math.floor(Date.now() / 1000 - timestamp);
        if (diff < 0) return 'Just now';
        if (diff < SECONDS_PER_MINUTE) return diff + 's ago';
        if (diff < SECONDS_PER_HOUR) return Math.floor(diff / SECONDS_PER_MINUTE) + 'm ago';
        return Math.floor(diff / SECONDS_PER_HOUR) + 'h ago';
    }

    // ---------------------------------------------------------------------------
    // Health Assessment
    // ---------------------------------------------------------------------------

    /**
     * Determine combined health status class for a validator.
     * @param {Object} data - Validator data from API
     * @returns {string} One of: 'active', 'warning', 'critical', 'inactive'
     */
    function getHealthStatus(data) {
        var healthy = data.healthy !== false;
        var fails = data.fails || 0;
        var warnings = data.warnings || [];
        var criticals = data.criticals || [];
        var validatorState = data.state || 'unknown';

        if (!healthy || criticals.length > 0) {
            return 'critical';
        }
        if ((fails > 0 && fails < FAIL_THRESHOLD) || warnings.length > 0) {
            return 'warning';
        }
        if (fails >= FAIL_THRESHOLD) {
            return 'critical';
        }
        if (validatorState === 'active') {
            return 'active';
        }
        return 'inactive';
    }

    /**
     * Get status display text.
     * @param {string} healthStatus
     * @returns {string}
     */
    function getStatusText(healthStatus) {
        return healthStatus.toUpperCase();
    }

    /**
     * Get network display name.
     * @param {string} network
     * @returns {string}
     */
    function getNetworkDisplayName(network) {
        if (!network) return '';
        return network.charAt(0).toUpperCase() + network.slice(1);
    }

    // ---------------------------------------------------------------------------
    // Animated Number Transitions
    // ---------------------------------------------------------------------------

    /**
     * Animate a numeric value change on an element using requestAnimationFrame.
     * Uses ease-in-out-quad easing.
     * @param {HTMLElement} element
     * @param {number|null} oldVal
     * @param {number} newVal
     * @param {number} [duration=300]
     * @param {Function} [formatter=formatNumber]
     */
    function animateValue(element, oldVal, newVal, duration, formatter) {
        if (duration === undefined) duration = ANIMATION_DURATION_MS;
        if (formatter === undefined) formatter = formatNumber;

        if (oldVal === newVal || oldVal === null || oldVal === undefined) {
            element.textContent = formatter(newVal);
            return;
        }
        if (newVal === null || newVal === undefined || isNaN(newVal)) {
            element.textContent = formatter(newVal);
            return;
        }

        var startTime = performance.now();
        var range = newVal - oldVal;

        function step(currentTime) {
            var elapsed = currentTime - startTime;
            var progress = Math.min(elapsed / duration, 1);
            // Ease-in-out-quad
            var eased = progress < 0.5
                ? 2 * progress * progress
                : 1 - Math.pow(-2 * progress + 2, 2) / 2;
            element.textContent = formatter(Math.round(oldVal + range * eased));
            if (progress < 1) {
                requestAnimationFrame(step);
            }
        }
        requestAnimationFrame(step);
    }

    // ---------------------------------------------------------------------------
    // Change Detection & Flash
    // ---------------------------------------------------------------------------

    /**
     * Add a flash class to an element and remove it after FLASH_DURATION_MS.
     * @param {HTMLElement} el
     */
    function flashElement(el) {
        if (!el) return;
        el.classList.add('metric-flash');
        setTimeout(function() {
            el.classList.remove('metric-flash');
        }, FLASH_DURATION_MS);
    }

    /**
     * Compare previous and current data for a validator and flash changed metrics.
     * Also triggers animated number transitions.
     * @param {HTMLElement} card
     * @param {Object} prev - Previous data snapshot
     * @param {Object} curr - Current data
     */
    function detectChangesAndAnimate(card, prev, curr) {
        var checks = [
            { key: 'height', selector: '.metric-height', formatter: formatNumber },
            { key: 'peers', selector: '.metric-peers', formatter: formatNumber },
        ];

        checks.forEach(function(check) {
            var oldVal = prev ? prev[check.key] : null;
            var newVal = curr[check.key];
            var el = card.querySelector(check.selector);
            if (!el) return;

            if (oldVal !== null && oldVal !== undefined && oldVal !== newVal) {
                flashElement(el);
            }
            animateValue(el, oldVal, newVal, ANIMATION_DURATION_MS, check.formatter);
        });

        // Uptime - not animated (it's a percent with decimals), but flash on change
        var oldUptime = prev && prev.huginn_data
            ? (prev.huginn_data.uptime_percent) : null;
        var newUptime = curr.huginn_data
            ? (curr.huginn_data.uptime_percent) : null;
        if (oldUptime !== null && oldUptime !== undefined &&
            newUptime !== null && newUptime !== undefined &&
            oldUptime !== newUptime) {
            flashElement(card.querySelector('.metric-uptime'));
        }

        // System metrics flash
        var oldSys = prev ? prev.system_metrics : null;
        var newSys = curr.system_metrics;
        if (oldSys && newSys) {
            ['cpu_used_percent', 'mem_percent', 'disk_percent'].forEach(function(key) {
                var resourceMap = {
                    cpu_used_percent: 'cpu',
                    mem_percent: 'memory',
                    disk_percent: 'disk'
                };
                if (oldSys[key] !== newSys[key]) {
                    var gauge = card.querySelector(
                        '.resource-gauge[data-resource="' + resourceMap[key] + '"]'
                    );
                    if (gauge) flashElement(gauge);
                }
            });
        }

        // Fails flash
        var oldFails = prev ? prev.fails : null;
        var newFails = curr.fails || 0;
        if (oldFails !== null && oldFails !== undefined && oldFails !== newFails) {
            flashElement(card.querySelector('.metric-fails'));
        }
    }

    // ---------------------------------------------------------------------------
    // SVG Sparklines
    // ---------------------------------------------------------------------------

    /**
     * Create a small inline SVG sparkline from an array of data points.
     * @param {number[]} dataPoints
     * @param {string} gradientId - Unique gradient ID
     * @returns {SVGElement|null}
     */
    function createSparklineSVG(dataPoints, gradientId) {
        if (!dataPoints || dataPoints.length < 2) return null;

        var w = 60;
        var h = 20;
        var min = Math.min.apply(null, dataPoints);
        var max = Math.max.apply(null, dataPoints);
        var range = max - min || 1;

        var points = dataPoints.map(function(v, i) {
            var x = (i / (dataPoints.length - 1)) * w;
            var y = h - ((v - min) / range) * (h - 2) - 1;
            return x.toFixed(1) + ',' + y.toFixed(1);
        });

        var ns = 'http://www.w3.org/2000/svg';

        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('class', 'sparkline');
        svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
        svg.setAttribute('width', '60');
        svg.setAttribute('height', '20');

        // Gradient definition (unique per sparkline)
        var defs = document.createElementNS(ns, 'defs');
        var grad = document.createElementNS(ns, 'linearGradient');
        grad.setAttribute('id', gradientId);
        grad.setAttribute('x1', '0');
        grad.setAttribute('y1', '0');
        grad.setAttribute('x2', '0');
        grad.setAttribute('y2', '1');

        var stop1 = document.createElementNS(ns, 'stop');
        stop1.setAttribute('offset', '0%');
        stop1.setAttribute('stop-color', 'rgba(133,230,255,0.3)');

        var stop2 = document.createElementNS(ns, 'stop');
        stop2.setAttribute('offset', '100%');
        stop2.setAttribute('stop-color', 'rgba(133,230,255,0)');

        grad.appendChild(stop1);
        grad.appendChild(stop2);
        defs.appendChild(grad);
        svg.appendChild(defs);

        // Area fill under line
        var areaPath = document.createElementNS(ns, 'path');
        areaPath.setAttribute(
            'd',
            'M0,' + h + ' L' + points.join(' L') + ' L' + w + ',' + h + ' Z'
        );
        areaPath.setAttribute('fill', 'url(#' + gradientId + ')');
        svg.appendChild(areaPath);

        // Line
        var path = document.createElementNS(ns, 'path');
        path.setAttribute('d', 'M' + points.join(' L'));
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', '#85E6FF');
        path.setAttribute('stroke-width', '1.5');
        path.setAttribute('stroke-linecap', 'round');
        path.setAttribute('stroke-linejoin', 'round');
        svg.appendChild(path);

        return svg;
    }

    /**
     * Update sparkline for a validator card.
     * @param {HTMLElement} card
     * @param {string} validatorName
     * @param {number|null|undefined} height
     */
    function updateSparkline(card, validatorName, height) {
        if (height === null || height === undefined) return;

        if (!state.heightHistory[validatorName]) {
            state.heightHistory[validatorName] = [];
        }
        var history = state.heightHistory[validatorName];
        history.push(height);
        if (history.length > HEIGHT_HISTORY_SIZE) {
            history.shift();
        }

        var heightEl = card.querySelector('.metric-height');
        if (!heightEl) return;

        // Remove existing sparkline
        var existingSparkline = heightEl.parentElement.querySelector('.sparkline');
        if (existingSparkline) {
            existingSparkline.remove();
        }

        sparklineIdCounter++;
        var gradientId = 'sparkline-grad-' + sparklineIdCounter;
        var svg = createSparklineSVG(history, gradientId);
        if (svg) {
            heightEl.parentElement.appendChild(svg);
        }
    }

    // ---------------------------------------------------------------------------
    // Resource Gauge Updates
    // ---------------------------------------------------------------------------

    /**
     * Update a resource gauge element.
     * @param {HTMLElement} card
     * @param {string} resource - 'cpu', 'memory', or 'disk'
     * @param {number|null|undefined} percent
     */
    function updateGauge(card, resource, percent) {
        var gauge = card.querySelector(
            '.resource-gauge[data-resource="' + resource + '"]'
        );
        if (!gauge) return;

        var gaugeValue = gauge.querySelector('.gauge-value');
        var gaugeFill = gauge.querySelector('.gauge-fill');
        if (!gaugeValue || !gaugeFill) return;

        if (percent === null || percent === undefined || isNaN(percent)) {
            gaugeValue.textContent = '--';
            gaugeFill.style.width = '0%';
            gaugeFill.classList.remove('gauge-warning', 'gauge-critical');
            return;
        }

        var clamped = Math.max(0, Math.min(GAUGE_MAX, percent));
        gaugeValue.textContent = Math.round(clamped) + '%';
        gaugeFill.style.width = clamped + '%';

        gaugeFill.classList.remove('gauge-warning', 'gauge-critical');
        if (clamped >= GAUGE_CRITICAL_MIN) {
            gaugeFill.classList.add('gauge-critical');
        } else if (clamped >= GAUGE_WARNING_MIN) {
            gaugeFill.classList.add('gauge-warning');
        }
    }

    // ---------------------------------------------------------------------------
    // Card Details (Expand/Collapse)
    // ---------------------------------------------------------------------------

    /**
     * Populate the expandable detail section of a card.
     * @param {HTMLElement} card
     * @param {Object} data - Validator data
     */
    function populateCardDetails(card, data) {
        var huginn = data.huginn_data;
        var blockProd = data.block_production;
        var systemMetrics = data.system_metrics;

        // Block production details
        setTextSafe(card, '.detail-proposals',
            blockProd ? formatNumber(blockProd.proposals) : '--');
        setTextSafe(card, '.detail-finalized',
            huginn ? formatNumber(huginn.finalized_count) : '--');
        setTextSafe(card, '.detail-timeouts',
            huginn ? formatNumber(huginn.timeout_count) : '--');

        // RPC health
        setTextSafe(card, '.detail-rpc',
            data.rpc_healthy === true ? 'Healthy'
                : data.rpc_healthy === false ? 'Down' : '--');

        // Network TPS
        setTextSafe(card, '.detail-tps',
            data.network_tps != null ? formatTps(data.network_tps) : '--');

        // TrieDB gauge
        var triedbContainer = card.querySelector('.detail-triedb');
        if (triedbContainer) {
            var triedbPercent = systemMetrics
                ? systemMetrics.triedb_used_percent : null;
            if (triedbPercent !== null && triedbPercent !== undefined) {
                triedbContainer.removeAttribute('hidden');
                setTextSafe(card, '.detail-triedb-value',
                    Math.round(triedbPercent) + '%');
                var triedbFill = card.querySelector('.detail-triedb-fill');
                if (triedbFill) {
                    triedbFill.style.width =
                        Math.max(0, Math.min(GAUGE_MAX, triedbPercent)) + '%';
                }
            } else {
                triedbContainer.setAttribute('hidden', '');
            }
        }

        // Warnings/criticals section
        var warningsContainer = card.querySelector('.detail-warnings');
        if (warningsContainer) {
            var warnings = data.warnings || [];
            var criticals = data.criticals || [];
            var allAlerts = criticals.concat(warnings);
            if (allAlerts.length > 0) {
                warningsContainer.removeAttribute('hidden');
                var warningsList = card.querySelector('.warnings-list');
                if (warningsList) {
                    warningsList.innerHTML = allAlerts.map(function(msg) {
                        return '<div class="warning-item">' +
                            escapeHtml(String(msg)) + '</div>';
                    }).join('');
                }
            } else {
                warningsContainer.setAttribute('hidden', '');
            }
        }
    }

    /**
     * Safely set textContent on a child element.
     * @param {HTMLElement} parent
     * @param {string} selector
     * @param {string} text
     */
    function setTextSafe(parent, selector, text) {
        var el = parent.querySelector(selector);
        if (el) {
            el.textContent = text;
        }
    }

    /**
     * Escape HTML entities to prevent injection.
     * @param {string} str
     * @returns {string}
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // ---------------------------------------------------------------------------
    // Card Creation & Update
    // ---------------------------------------------------------------------------

    /**
     * Create a new validator card from template.
     * @param {string} name
     * @param {Object} data
     * @param {number} index - Card index for stagger animation
     * @returns {HTMLElement}
     */
    function createValidatorCard(name, data, index) {
        var template = elements.cardTemplate;
        var card = template.content.cloneNode(true).querySelector('.validator-card');

        card.dataset.validatorName = name;
        card.setAttribute('tabindex', '0');
        card.style.animationDelay = (index * CARD_STAGGER_DELAY_SEC) + 's';

        applyCardData(card, name, data);

        return card;
    }

    /**
     * Apply all data to a card (used for both create and update).
     * @param {HTMLElement} card
     * @param {string} name
     * @param {Object} data
     */
    function applyCardData(card, name, data) {
        var healthStatus = getHealthStatus(data);
        var statusText = getStatusText(healthStatus);
        var validatorState = data.state || 'unknown';
        var network = data.network || 'testnet';

        // Status classes on card itself
        card.classList.remove(
            'status-active', 'status-warning', 'status-critical', 'status-inactive'
        );
        card.classList.add('status-' + healthStatus);

        // Validator name
        setTextSafe(card, '.validator-name', name);

        // Network badge
        var networkBadge = card.querySelector('.validator-network-badge');
        if (networkBadge) {
            networkBadge.textContent = getNetworkDisplayName(network);
            networkBadge.className = 'validator-network-badge ' + network;
            networkBadge.setAttribute(
                'aria-label', 'Network: ' + getNetworkDisplayName(network)
            );
        }


        // Status indicator dot
        var indicator = card.querySelector('.validator-indicator');
        if (indicator) {
            indicator.className = 'validator-indicator ' + healthStatus;
        }

        // Health status badge
        var badge = card.querySelector('.validator-status-badge');
        if (badge) {
            badge.className = 'validator-status-badge ' + healthStatus;
            badge.textContent = statusText;
        }

        // Accessible label
        card.setAttribute(
            'aria-label',
            name + ', ' + statusText + ', ' + getNetworkDisplayName(network)
        );

        // Uptime
        var uptimeEl = card.querySelector('.metric-uptime');
        if (uptimeEl) {
            if (data.huginn_data &&
                data.huginn_data.uptime_percent !== undefined &&
                data.huginn_data.uptime_percent !== null) {
                uptimeEl.textContent =
                    formatUptimePercent(data.huginn_data.uptime_percent);
                uptimeEl.title =
                    'Finalized: ' + (data.huginn_data.finalized_count || 0) +
                    ' / Timeouts: ' + (data.huginn_data.timeout_count || 0);
            } else {
                uptimeEl.textContent = 'N/A';
                uptimeEl.title = 'Huginn data not available';
            }
        }

        // Fails
        var failsEl = card.querySelector('.metric-fails');
        if (failsEl) {
            var fails = data.fails || 0;
            failsEl.textContent = fails;
            if (fails > 0) {
                failsEl.classList.add('has-fails');
            } else {
                failsEl.classList.remove('has-fails');
            }
        }

        // Resource gauges
        var sys = data.system_metrics;
        updateGauge(card, 'cpu', sys ? sys.cpu_used_percent : null);
        updateGauge(card, 'memory', sys ? sys.mem_percent : null);
        updateGauge(card, 'disk', sys ? sys.disk_percent : null);

        // Last check
        var lastCheckEl = card.querySelector('.last-check-time');
        if (lastCheckEl && data.last_check) {
            lastCheckEl.textContent = formatTimeAgo(data.last_check);
        }

        // Populate detail section
        populateCardDetails(card, data);
    }

    /**
     * Update an existing validator card with new data and animations.
     * @param {HTMLElement} card
     * @param {string} name
     * @param {Object} data
     */
    function updateValidatorCard(card, name, data) {
        var prev = state.previousData[name] || null;

        // Detect changes and animate metrics
        detectChangesAndAnimate(card, prev, data);

        // Apply all other data
        applyCardData(card, name, data);

        // Update sparkline
        updateSparkline(card, name, data.height);

        // Remove error state
        card.classList.remove('error');
    }

    // ---------------------------------------------------------------------------
    // Summary Bar
    // ---------------------------------------------------------------------------

    /**
     * Update the summary bar with aggregated validator counts and network metrics.
     * @param {Object} data - Full health endpoint response
     */
    function updateSummaryBar(data) {
        var validators = data.validators || {};
        var entries = Object.values(validators);
        var total = entries.length;
        var activeCount = 0;
        var warningCount = 0;
        var criticalCount = 0;

        entries.forEach(function(v) {
            var status = getHealthStatus(v);
            if (status === 'active') {
                activeCount++;
            } else if (status === 'warning') {
                warningCount++;
            } else if (status === 'critical') {
                criticalCount++;
            }
            // inactive validators are not counted in any bucket
        });

        if (elements.totalValidators) {
            elements.totalValidators.textContent = total;
        }
        if (elements.activeCount) {
            elements.activeCount.textContent = activeCount;
        }
        if (elements.warningCount) {
            elements.warningCount.textContent = warningCount;
        }
        if (elements.criticalCount) {
            elements.criticalCount.textContent = criticalCount;
        }

        // Monitor uptime
        if (elements.monitorUptime) {
            elements.monitorUptime.textContent =
                formatUptime(data.uptime_seconds);
        }
    }

    // ---------------------------------------------------------------------------
    // Connection Status
    // ---------------------------------------------------------------------------

    /**
     * Update connection status UI.
     * @param {boolean} connected
     */
    function updateConnectionStatus(connected) {
        state.isConnected = connected;
        if (elements.connectionDot) {
            elements.connectionDot.className = connected
                ? 'connection-dot connected'
                : 'connection-dot disconnected';
        }
        if (elements.connectionStatus) {
            elements.connectionStatus.textContent = connected
                ? 'Connected' : 'Disconnected';
        }
        if (connected) {
            state.reconnectAttempts = 0;
        }
    }

    // ---------------------------------------------------------------------------
    // Render Validators
    // ---------------------------------------------------------------------------

    /**
     * Render all validators from health data.
     * @param {Object} data - Full health endpoint response
     */
    function renderValidators(data) {
        var validators = data.validators || {};
        var existingCards = new Map();

        elements.validatorsContainer
            .querySelectorAll('.validator-card')
            .forEach(function(card) {
                existingCards.set(card.dataset.validatorName, card);
            });

        var cardIndex = 0;

        Object.entries(validators).forEach(function(entry) {
            var name = entry[0];
            var validatorData = entry[1];
            var existingCard = existingCards.get(name);

            // Store per-validator state
            state.validators[name] = {
                lastCheck: validatorData.last_check,
                huginnData: validatorData.huginn_data
            };

            if (existingCard) {
                updateValidatorCard(existingCard, name, validatorData);
                existingCards.delete(name);
            } else {
                var newCard = createValidatorCard(name, validatorData, cardIndex);
                // Initial sparkline population
                updateSparkline(newCard, name, validatorData.height);
                // Set initial height/peers text (no animation for first render)
                setTextSafe(newCard, '.metric-height', formatNumber(validatorData.height));
                setTextSafe(newCard, '.metric-peers', formatNumber(validatorData.peers));
                elements.validatorsContainer.appendChild(newCard);
            }

            // Store current data as previous for next cycle
            state.previousData[name] = cloneValidatorData(validatorData);
            cardIndex++;
        });

        // Remove cards for validators no longer in response
        existingCards.forEach(function(card, name) {
            card.remove();
            delete state.previousData[name];
            delete state.heightHistory[name];
            delete state.validators[name];
        });

        // Single validator layout class
        var validatorCount = Object.keys(validators).length;
        if (validatorCount === 1) {
            elements.validatorsContainer.classList.add('has-single-validator');
        } else {
            elements.validatorsContainer.classList.remove('has-single-validator');
        }

        state.lastUpdate = Date.now();
    }

    /**
     * Create a shallow clone of validator data for change detection.
     * @param {Object} data
     * @returns {Object}
     */
    function cloneValidatorData(data) {
        var clone = {
            height: data.height,
            peers: data.peers,
            fails: data.fails,
            healthy: data.healthy,
            state: data.state,
        };
        if (data.huginn_data) {
            clone.huginn_data = {
                uptime_percent: data.huginn_data.uptime_percent
            };
        }
        if (data.system_metrics) {
            clone.system_metrics = {
                cpu_used_percent: data.system_metrics.cpu_used_percent,
                mem_percent: data.system_metrics.mem_percent,
                disk_percent: data.system_metrics.disk_percent
            };
        }
        return clone;
    }

    // ---------------------------------------------------------------------------
    // Error Handling
    // ---------------------------------------------------------------------------

    /**
     * Handle fetch/connection errors.
     * @param {Error} error
     */
    function handleError(error) {
        console.error('Health check failed:', error.message || error);
        updateConnectionStatus(false);

        elements.validatorsContainer
            .querySelectorAll('.validator-card')
            .forEach(function(card) {
                card.classList.add('error');
            });

        if (state.reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            state.reconnectAttempts++;
            console.log(
                'Reconnection attempt ' + state.reconnectAttempts +
                '/' + MAX_RECONNECT_ATTEMPTS
            );
        }
    }

    // ---------------------------------------------------------------------------
    // Data Fetching
    // ---------------------------------------------------------------------------

    /**
     * Fetch health data from the backend endpoint.
     */
    async function fetchHealth() {
        try {
            var response = await fetch(HEALTH_ENDPOINT, {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            });

            if (!response.ok) {
                throw new Error('HTTP ' + response.status + ': ' + response.statusText);
            }

            var data = await response.json();
            updateConnectionStatus(true);
            updateSummaryBar(data);
            renderValidators(data);


        } catch (error) {
            handleError(error);
        }
    }

    // ---------------------------------------------------------------------------
    // Polling
    // ---------------------------------------------------------------------------

    /**
     * Start the polling loop.
     */
    function startPolling() {
        fetchHealth();
        state.pollTimer = setInterval(fetchHealth, POLLING_INTERVAL_MS);
    }

    /**
     * Stop polling.
     */
    function stopPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    // ---------------------------------------------------------------------------
    // Keyboard Navigation
    // ---------------------------------------------------------------------------

    /**
     * Get all validator cards in DOM order.
     * @returns {HTMLElement[]}
     */
    function getAllCards() {
        return Array.from(
            elements.validatorsContainer.querySelectorAll('.validator-card')
        );
    }

    /**
     * Handle keyboard navigation between validator cards.
     * @param {KeyboardEvent} evt
     */
    function handleKeyboardNavigation(evt) {
        var cards = getAllCards();
        if (cards.length === 0) return;

        var activeElement = document.activeElement;
        var currentIndex = cards.indexOf(activeElement);

        if (evt.key === 'ArrowDown' || evt.key === 'ArrowUp') {
            evt.preventDefault();
            var nextIndex;
            if (currentIndex === -1) {
                nextIndex = 0;
            } else if (evt.key === 'ArrowDown') {
                nextIndex = Math.min(currentIndex + 1, cards.length - 1);
            } else {
                nextIndex = Math.max(currentIndex - 1, 0);
            }
            cards[nextIndex].focus();
        }

    }

    // ---------------------------------------------------------------------------
    // Initialization
    // ---------------------------------------------------------------------------

    /**
     * Initialize the dashboard application.
     */
    function init() {
        console.log('Monad Validator Monitor Dashboard initializing...');

        if (!elements.validatorsContainer || !elements.cardTemplate) {
            console.error('Required DOM elements not found');
            return;
        }

        startPolling();

        // Pause polling when tab is hidden, resume when visible
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                stopPolling();
            } else {
                startPolling();
            }
        });

        // Keyboard navigation
        document.addEventListener('keydown', handleKeyboardNavigation);

        console.log('Dashboard initialized successfully');
    }

    // Bootstrap
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
