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

RECAPTCHA_EXECUTE_SOURCE = r"""(async (siteKey, requestedAction, timeoutMs) => {
            const storageKey = '__flow2apiTrustedRecaptchaExecute';
            for (let attempt = 0; attempt < 40; attempt += 1) {
                if (window[storageKey]) break;
                await new Promise(resolve => setTimeout(resolve, 250));
            }

            const capturedExecute = !!window[storageKey];
            let execute = window[storageKey];
            let executeSource = '';
            try {
                executeSource = String(execute || '');
            } catch (error) {}

            if (!execute) {
                const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
                execute = enterprise && enterprise.execute;
                try {
                    executeSource = String(execute || '');
                } catch (error) {}
                if (
                    executeSource.includes('extension_hijack_detected') &&
                    !window.__flow2apiRecaptchaActionGuardInstalled
                ) {
                    return {
                        ok: false,
                        error: 'trusted reCAPTCHA action guard is unavailable',
                        debug: {
                            hookInstalled: !!window.__flow2apiTrustedRecaptchaHookInstalled,
                            actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                            captured: !!window.__flow2apiTrustedRecaptchaExecute,
                            url: location.href
                        }
                    };
                }
            }

            if (typeof execute !== 'function') {
                return { ok: false, error: 'reCAPTCHA execute is unavailable' };
            }

            let executeTarget = siteKey;
            try {
                if (
                    !capturedExecute &&
                    window.grecaptcha &&
                    window.grecaptcha.enterprise &&
                    typeof window.grecaptcha.enterprise.render === 'function' &&
                    window.__flow2apiRecaptchaClientId === undefined
                ) {
                    const holder = document.createElement('div');
                    holder.style.display = 'none';
                    (document.body || document.documentElement).appendChild(holder);
                    window.__flow2apiRecaptchaClientId =
                        window.grecaptcha.enterprise.render(
                            holder,
                            { sitekey: siteKey, size: 'invisible', action: requestedAction }
                        );
                }
                if (window.__flow2apiRecaptchaClientId !== undefined) {
                    executeTarget = window.__flow2apiRecaptchaClientId;
                }
            } catch (renderError) {
                return {
                    ok: false,
                    error: renderError && renderError.message
                        ? renderError.message
                        : String(renderError)
                };
            }

            try {
                const token = await new Promise((resolve, reject) => {
                    let settled = false;
                    const finish = (handler, value) => {
                        if (settled) return;
                        settled = true;
                        handler(value);
                    };
                    const timer = setTimeout(() => {
                        finish(reject, new Error('execute timeout'));
                    }, timeoutMs);
                    try {
                        Promise.resolve(execute(executeTarget, { action: requestedAction }))
                            .then(value => {
                                clearTimeout(timer);
                                finish(resolve, value);
                            })
                            .catch(error => {
                                clearTimeout(timer);
                                finish(reject, error);
                            });
                    } catch (error) {
                        clearTimeout(timer);
                        finish(reject, error);
                    }
                });
                return { ok: true, token };
            } catch (error) {
                const debug = {
                    captured: capturedExecute,
                    actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                    renderType: window.grecaptcha && window.grecaptcha.enterprise
                        ? typeof window.grecaptcha.enterprise.render
                        : 'missing',
                    clientId: String(window.__flow2apiRecaptchaClientId),
                    executeTarget: String(executeTarget),
                    url: location.href
                };
                return {
                    ok: false,
                    error: (error && error.message ? error.message : String(error))
                        + ' | ' + JSON.stringify(debug)
                };
            }
        })(__SITE_KEY__, __REQUESTED_ACTION__, __TIMEOUT_MS__)"""


def build_recaptcha_execute_script(site_key: str, action: str, timeout_ms: int) -> str:
    return (
        RECAPTCHA_EXECUTE_SOURCE
        .replace("__SITE_KEY__", json.dumps(site_key))
        .replace("__REQUESTED_ACTION__", json.dumps(action))
        .replace("__TIMEOUT_MS__", json.dumps(timeout_ms))
    )
