"""Shared reCAPTCHA execution helpers for local browser providers."""

import json


TRUSTED_RECAPTCHA_HOOK_SOURCE = r"""
(() => {
    if (window.__flow2apiTrustedRecaptchaHookInstalled) return;
    Object.defineProperty(
        window,
        '__flow2apiTrustedRecaptchaHookInstalled',
        { value: true }
    );

    const storageKey = '__flow2apiTrustedRecaptchaExecute';
    const originalBind = Function.prototype.bind;
    Function.prototype.bind = function(thisArg, ...args) {
        const bound = originalBind.call(this, thisArg, ...args);
        try {
            const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
            if (
                !window[storageKey] &&
                enterprise &&
                this === enterprise.execute &&
                typeof bound === 'function'
            ) {
                window[storageKey] = bound;
            }
        } catch (error) {}
        return bound;
    };

    // New Flow replaces the public execute wrapper with an assignment that
    // overwrites the caller action by extension_hijack_detected. Preserve the
    // requested action when that exact assignment shape is used.
    Object.defineProperty(
        window,
        '__flow2apiRecaptchaActionGuardInstalled',
        { value: true }
    );
    const originalAssign = Object.assign;
    Object.assign = function(target, ...sources) {
        let nextSources = sources;
        if (sources.length >= 2) {
            const previous = sources[sources.length - 2];
            const last = sources[sources.length - 1];
            if (
                previous &&
                typeof previous.action === 'string' &&
                previous.action &&
                last &&
                last.action === 'extension_hijack_detected'
            ) {
                nextSources = sources.slice(0, -1);
            }
        }
        return originalAssign.call(this, target, ...nextSources);
    };
})();
"""

# Execution strategy (in order):
#   1. the pre-hijack `enterprise.execute` closure captured by the bind hook
#      (older Flow builds),
#   2. `grecaptcha.enterprise.execute.call(enterprise, siteKey, {action})`
#      (newer Flow builds no longer call `.bind()`, so nothing gets captured;
#      this is the documented enterprise-v3 usage and works without a render),
#   3. render into a *fresh empty* container and execute the widget id. The old
#      code rendered into `document.documentElement`, which always contains
#      children and makes reCAPTCHA throw
#      "reCAPTCHA placeholder element must be empty".
RECAPTCHA_EXECUTE_SOURCE = r"""(async (siteKey, requestedAction, timeoutMs) => {
            const storageKey = '__flow2apiTrustedRecaptchaExecute';

            for (let attempt = 0; attempt < 20; attempt += 1) {
                if (window[storageKey]) break;
                await new Promise(resolve => setTimeout(resolve, 250));
            }

            const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
            const capturedExecute = !!window[storageKey];
            const debug = {
                hookInstalled: !!window.__flow2apiTrustedRecaptchaHookInstalled,
                actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                captured: capturedExecute,
                executeType: enterprise ? typeof enterprise.execute : 'missing',
                renderType: enterprise ? typeof enterprise.render : 'missing',
                url: location.href
            };
            const errors = [];

            const runExecute = async (label, factory) => {
                try {
                    const token = await new Promise((resolve, reject) => {
                        let settled = false;
                        const finish = (handler, value) => {
                            if (settled) return;
                            settled = true;
                            clearTimeout(timer);
                            handler(value);
                        };
                        const timer = setTimeout(() => {
                            finish(reject, new Error('execute timeout'));
                        }, timeoutMs);
                        try {
                            Promise.resolve(factory()).then(
                                value => finish(resolve, value),
                                error => finish(reject, error)
                            );
                        } catch (error) {
                            finish(reject, error);
                        }
                    });
                    const normalized = typeof token === 'string' ? token.trim() : '';
                    if (normalized) return normalized;
                    errors.push(label + ': empty token');
                } catch (error) {
                    errors.push(
                        label + ': '
                        + (error && error.message ? error.message : String(error))
                    );
                }
                return null;
            };

            if (capturedExecute && typeof window[storageKey] === 'function') {
                const token = await runExecute('captured', () => {
                    return window[storageKey](siteKey, { action: requestedAction });
                });
                if (token) return { ok: true, token, debug };
            }

            if (enterprise && typeof enterprise.execute === 'function') {
                let executeSource = '';
                try {
                    executeSource = String(enterprise.execute || '');
                } catch (error) {}
                if (
                    executeSource.includes('extension_hijack_detected') &&
                    !window.__flow2apiRecaptchaActionGuardInstalled
                ) {
                    errors.push('direct: trusted reCAPTCHA action guard is unavailable');
                } else {
                    const token = await runExecute('direct', () => {
                        return enterprise.execute.call(
                            enterprise,
                            siteKey,
                            { action: requestedAction }
                        );
                    });
                    if (token) return { ok: true, token, debug };
                }

                if (typeof enterprise.render === 'function') {
                    try {
                        const container = document.createElement('div');
                        container.setAttribute('data-flow2api-recaptcha', '1');
                        container.style.display = 'none';
                        (document.body || document.documentElement)
                            .appendChild(container);
                        const clientId = enterprise.render(
                            container,
                            { sitekey: siteKey, action: requestedAction }
                        );
                        const token = await runExecute('render', () => {
                            return enterprise.execute.call(
                                enterprise,
                                clientId,
                                { action: requestedAction }
                            );
                        });
                        if (token) return { ok: true, token, debug };
                    } catch (renderError) {
                        errors.push(
                            'render: '
                            + (renderError && renderError.message
                                ? renderError.message
                                : String(renderError))
                        );
                    }
                }
            }

            return {
                ok: false,
                error: errors.join(' | ') || 'reCAPTCHA execute is unavailable',
                debug
            };
        })(__SITE_KEY__, __REQUESTED_ACTION__, __TIMEOUT_MS__)"""


def build_recaptcha_execute_script(site_key: str, action: str, timeout_ms: int) -> str:
    return (
        RECAPTCHA_EXECUTE_SOURCE
        .replace("__SITE_KEY__", json.dumps(site_key))
        .replace("__REQUESTED_ACTION__", json.dumps(action))
        .replace("__TIMEOUT_MS__", json.dumps(timeout_ms))
    )
