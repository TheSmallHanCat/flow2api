"""reCAPTCHA Enterprise 脚本辅助：供 browser_captcha 注入与执行。"""

from __future__ import annotations

import json
from typing import Tuple

FLOW_RECAPTCHA_WEBSITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"


def flow_project_page_url(project_id: str) -> str:
    project_id = (project_id or "").strip()
    return f"https://labs.google/fx/tools/flow/project/{project_id}"


def enterprise_script_urls(
    website_key: str,
    *,
    use_recaptcha_net: bool = False,
) -> Tuple[str, str]:
    primary_host = "https://www.recaptcha.net" if use_recaptcha_net else "https://www.google.com"
    secondary_host = (
        "https://www.google.com"
        if primary_host == "https://www.recaptcha.net"
        else "https://www.recaptcha.net"
    )
    path = f"/recaptcha/enterprise.js?render={website_key}"
    return f"{primary_host}{path}", f"{secondary_host}{path}"


def build_enterprise_wait_expression() -> str:
    return (
        "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
        "typeof grecaptcha.enterprise.execute === 'function'"
    )


def build_enterprise_execute_evaluator(website_key: str) -> str:
    key = website_key.replace("\\", "\\\\").replace("'", "\\'")
    return f"""
(actionName) => {{
    return new Promise((resolve, reject) => {{
        const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
        try {{
            grecaptcha.enterprise.ready(function() {{
                grecaptcha.enterprise.execute('{key}', {{action: actionName}})
                    .then((token) => {{
                        clearTimeout(timeout);
                        resolve(token);
                    }})
                    .catch((err) => {{
                        clearTimeout(timeout);
                        reject(err);
                    }});
            }});
        }} catch (error) {{
            clearTimeout(timeout);
            reject(error);
        }}
    }});
}}
""".strip()


def build_inject_script_loader_evaluator(primary_url: str, secondary_url: str) -> str:
    return """
(primaryUrl, secondaryUrl) => {
    const existing = Array.from(document.scripts || []).some((script) => {
        const src = script?.src || "";
        return src.includes('/recaptcha/');
    });
    if (existing) return;
    const urls = [primaryUrl, secondaryUrl];
    const loadScript = (index) => {
        if (index >= urls.length) return;
        const script = document.createElement('script');
        script.src = urls[index];
        script.async = true;
        script.onerror = () => loadScript(index + 1);
        document.head.appendChild(script);
    };
    loadScript(0);
}
""".strip()


def build_enterprise_bootstrap_html(
    website_key: str,
    *,
    use_recaptcha_net: bool = False,
) -> str:
    primary_url, secondary_url = enterprise_script_urls(
        website_key,
        use_recaptcha_net=use_recaptcha_net,
    )
    urls_json = json.dumps([primary_url, secondary_url])
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>reCAPTCHA</title></head>
<body>
<script>
(function() {{
  const urls = {urls_json};
  const loadScript = (index) => {{
    if (index >= urls.length) return;
    const script = document.createElement('script');
    script.src = urls[index];
    script.async = true;
    script.onerror = () => loadScript(index + 1);
    document.head.appendChild(script);
  }};
  loadScript(0);
}})();
</script>
</body>
</html>"""
