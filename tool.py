"""
tool.py - 最小化 LTI 1.3 Tool（模拟外部学习工具）
运行端口：8001

LTI 1.3 流程中 Tool 承担的角色：
  2. 接收 Platform 的 OIDC 登录请求，生成 nonce/state，重定向回 Platform 授权端点
  4. 接收 Platform POST 的 id_token，验证签名、nonce、iss、aud
  5. 解析 LTI claims，渲染对应内容
  *. （可选）通过 AGS 将成绩回传给 Platform
  *. （可选）通过 NRPS 拉取课程花名册
"""

import json
import uuid
from urllib.parse import urlencode

import jwt
import requests as http
from flask import Flask, jsonify, redirect, render_template_string, request, session

app = Flask(__name__)
# session 使用 Flask 默认的 cookie 签名机制存 nonce/state
app.secret_key = "tool-secret-CHANGE-IN-PRODUCTION"

# ---------- 已知 Platform 列表（生产环境存数据库）----------

REGISTERED_PLATFORMS = {
    "http://localhost:8000": {
        "client_id":    "tool-client-001",
        "auth_endpoint":"http://localhost:8000/lti/auth",
        "jwks_uri":     "http://localhost:8000/.well-known/jwks.json",
        "deployment_id":"deployment-001",
    }
}

TOOL_REDIRECT_URI = "http://localhost:8001/lti/launch"

# 已用过的 nonce 集合，防重放（单进程 dev 可用；生产用 Redis + TTL）
_USED_NONCES: set[str] = set()

# ---------- 工具函数 ----------

def _get_platform_public_key(jwks_uri: str, kid: str):
    """从 Platform JWKS 端点拉取公钥，按 kid 匹配。"""
    resp = http.get(jwks_uri, timeout=5)
    resp.raise_for_status()
    jwks = resp.json()
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            from jwt.algorithms import RSAAlgorithm
            return RSAAlgorithm.from_jwk(json.dumps(key_data))
    raise ValueError(f"JWKS 中未找到 kid={kid} 的公钥")


# ---------- Step 2: Tool 登录端点 ----------

@app.route("/lti/login", methods=["GET", "POST"])
def login():
    """
    Step 2: 接收 Platform 的 OIDC 第三方发起登录请求。

    不做任何验证，只生成 state 和 nonce 存入 session，
    然后重定向回 Platform 的授权端点。
    """
    iss             = request.values.get("iss")
    login_hint      = request.values.get("login_hint")
    lti_message_hint= request.values.get("lti_message_hint", "")
    client_id       = request.values.get("client_id")

    platform = REGISTERED_PLATFORMS.get(iss)
    if not platform:
        return f"未知 Platform iss={iss}", 400

    # 生成一次性 state 和 nonce
    state = str(uuid.uuid4())
    nonce = str(uuid.uuid4())

    # 写入 session，launch 端点用于验证
    session["lti_state"] = state
    session["lti_nonce"] = nonce
    session["lti_iss"]   = iss

    params = {
        "scope":            "openid",
        "response_type":    "id_token",
        "client_id":        client_id or platform["client_id"],
        "redirect_uri":     TOOL_REDIRECT_URI,
        "login_hint":       login_hint,
        "lti_message_hint": lti_message_hint,
        "state":            state,
        "nonce":            nonce,
        "response_mode":    "form_post",
        "prompt":           "none",
    }

    auth_url = f"{platform['auth_endpoint']}?{urlencode(params)}"
    print(f"[Tool] Step 2: 重定向到 Platform 授权端点")
    print(f"       {auth_url[:80]}...")

    return redirect(auth_url)


# ---------- Step 4 & 5: Tool Launch 端点 ----------

LAUNCH_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>{{ resource_title }} - LTI Tool</title>
  <style>
    body { font-family: -apple-system, 'PingFang SC', sans-serif;
           max-width: 760px; margin: 40px auto; padding: 0 20px; color: #333; }
    h1 { color: #07c160; border-bottom: 2px solid #07c160; padding-bottom: 8px; }
    .meta { background: #f7f8fa; border-radius: 8px; padding: 14px 18px; margin: 16px 0; }
    .meta span { margin-right: 24px; }
    .badge { background: #07c160; color: white; border-radius: 4px;
             padding: 2px 8px; font-size: 12px; }
    .badge.inst { background: #6366f1; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 16px;
          border-radius: 8px; font-size: 13px; overflow-x: auto; }
    .section { margin: 20px 0; }
    h2 { font-size: 16px; color: #555; border-left: 4px solid #07c160;
         padding-left: 10px; }
    button { background: #07c160; color: white; border: none; border-radius: 6px;
             padding: 10px 20px; font-size: 14px; cursor: pointer; margin-right: 8px; }
    button:hover { background: #059652; }
    button.secondary { background: #6366f1; }
    button.secondary:hover { background: #4f46e5; }
    #result { margin-top: 12px; padding: 10px 14px; border-radius: 6px;
              background: #f0faf0; border: 1px solid #07c160; display: none; }
    .flow { background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px;
            padding: 12px 16px; font-size: 13px; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="flow">
    LTI 1.3 握手完成：Platform 签名的 JWT 已验证通过，nonce 已消耗，用户身份已确认。
  </div>

  <h1>{{ resource_title }}</h1>

  <div class="meta">
    <span>课程：<strong>{{ course_title }}</strong></span>
    <span>用户：<strong>{{ user_name }}</strong></span>
    <span class="badge {% if is_instructor %}inst{% endif %}">
      {{ "Instructor" if is_instructor else "Learner" }}
    </span>
  </div>

  {% if is_instructor %}
  <div class="section">
    <h2>教师视图：学生列表（NRPS Demo）</h2>
    <button class="secondary" onclick="fetchRoster()">拉取花名册</button>
    <div id="roster-result"></div>
  </div>
  {% endif %}

  <div class="section">
    <h2>学习内容区域</h2>
    <p>（此处渲染实际课件、视频或编程题，本 demo 展示 JWT 解析结果）</p>
    <button onclick="submitGrade()">提交成绩（AGS Demo：95/100）</button>
    <div id="result"></div>
  </div>

  <div class="section">
    <h2>已解析的 LTI JWT Claims</h2>
    <pre>{{ claims_json }}</pre>
  </div>

  <script>
  const AGS_LINEITEM = "{{ ags_lineitem }}";
  const NRPS_URL     = "{{ nrps_url }}";
  const USER_ID      = "{{ user_id }}";

  async function submitGrade() {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = "提交中...";
    const res = await fetch("/api/submit-grade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lineitem: AGS_LINEITEM,
        userId: USER_ID,
        score: 95,
        max: 100
      })
    });
    const data = await res.json();
    const div = document.getElementById("result");
    div.style.display = "block";
    div.textContent = res.ok
      ? "成绩提交成功：" + JSON.stringify(data)
      : "提交失败：" + JSON.stringify(data);
    btn.disabled = false;
    btn.textContent = "提交成绩（AGS Demo：95/100）";
  }

  async function fetchRoster() {
    const res = await fetch("/api/roster?nrps_url=" + encodeURIComponent(NRPS_URL));
    const data = await res.json();
    document.getElementById("roster-result").innerHTML =
      "<pre>" + JSON.stringify(data, null, 2) + "</pre>";
  }
  </script>
</body>
</html>
"""


@app.route("/lti/launch", methods=["POST"])
def launch():
    """
    Step 4 & 5: 接收并验证 Platform POST 的 id_token，渲染工具内容。

    安全检查顺序：
      1. state 验证（防 CSRF）
      2. JWT 签名验证（用 Platform JWKS 公钥）
      3. iss / aud / exp 验证（PyJWT 自动完成）
      4. nonce 验证（防重放）
      5. deployment_id 验证
    """
    id_token = request.form.get("id_token")
    state    = request.form.get("state")

    # ---- 1. 验证 state ----
    if not state or state != session.get("lti_state"):
        return "state 不匹配，可能存在 CSRF", 403

    iss = session.get("lti_iss")
    platform = REGISTERED_PLATFORMS.get(iss)
    if not platform:
        return f"未知 Platform: {iss}", 400

    # ---- 2. 获取 Platform 公钥 ----
    try:
        unverified_header = jwt.get_unverified_header(id_token)
        kid = unverified_header.get("kid")
        public_key = _get_platform_public_key(platform["jwks_uri"], kid)
    except Exception as e:
        return f"获取公钥失败: {e}", 400

    # ---- 3. 验证签名、iss、aud、exp ----
    try:
        claims = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=platform["client_id"],
            issuer=iss,
        )
    except jwt.ExpiredSignatureError:
        return "JWT 已过期", 403
    except jwt.InvalidAudienceError:
        return "JWT audience 不匹配", 403
    except jwt.InvalidIssuerError:
        return "JWT issuer 不匹配", 403
    except jwt.InvalidTokenError as e:
        return f"JWT 验证失败: {e}", 403

    # ---- 4. 验证 nonce（一次性使用）----
    nonce = claims.get("nonce")
    if not nonce or nonce != session.get("lti_nonce"):
        return "nonce 不匹配", 403
    if nonce in _USED_NONCES:
        return "nonce 已被使用，疑似重放攻击", 403
    _USED_NONCES.add(nonce)

    # ---- 5. 验证 deployment_id ----
    deployment_id = claims.get(
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
    )
    if deployment_id != platform["deployment_id"]:
        return f"deployment_id 不匹配: {deployment_id}", 403

    print(f"[Tool] Step 4 & 5: JWT 验证通过，用户 sub={claims.get('sub')}")

    # ---- 解析 LTI claims ----
    roles        = claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])
    context      = claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {})
    resource_link= claims.get("https://purl.imsglobal.org/spec/lti/claim/resource_link", {})
    ags          = claims.get("https://purl.imsglobal.org/spec/lti-ags/claim/endpoint", {})
    nrps         = claims.get("https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice", {})

    is_instructor = any("Instructor" in r for r in roles)

    # 只展示 LTI 相关 claim（过滤掉标准 OIDC 字段）
    lti_claims = {k: v for k, v in claims.items()
                  if "purl.imsglobal" in k or k in ("sub", "name", "email", "nonce")}

    return render_template_string(
        LAUNCH_PAGE,
        resource_title = resource_link.get("title", "Learning Resource"),
        course_title   = context.get("title", "Unknown Course"),
        user_name      = claims.get("name", claims.get("sub")),
        user_id        = claims.get("sub", ""),
        is_instructor  = is_instructor,
        ags_lineitem   = ags.get("lineitem", ""),
        nrps_url       = nrps.get("context_memberships_url", ""),
        claims_json    = json.dumps(lti_claims, indent=2, ensure_ascii=False),
    )


# ---------- AGS：提交成绩到 Platform ----------

@app.route("/api/submit-grade", methods=["POST"])
def submit_grade():
    """
    通过 AGS 向 Platform 提交成绩。

    生产环境需要先用 OAuth2 Client Credentials 换取 Bearer token：
      POST {platform_token_endpoint}
        grant_type=client_credentials
        client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
        client_assertion={tool_signed_jwt}
        scope=https://purl.imsglobal.org/spec/lti-ags/scope/score

    本 demo 为简化演示，直接 POST 不带 token（Platform 侧也跳过验证）。
    """
    data        = request.json
    lineitem    = data.get("lineitem")
    user_id     = data.get("userId")
    score_given = data.get("score", 0)
    score_max   = data.get("max", 100)

    if not lineitem:
        return jsonify({"error": "lineitem URL 为空"}), 400

    score_payload = {
        "userId":           user_id,
        "scoreGiven":       score_given,
        "scoreMaximum":     score_max,
        "activityProgress": "Completed",
        "gradingProgress":  "FullyGraded",
        "timestamp":        "2026-06-15T10:30:00Z",
        "comment":          "Submitted via LTI AGS demo",
    }

    print(f"[Tool AGS] 回传成绩 -> {lineitem}/scores")
    print(f"           payload: {score_payload}")

    resp = http.post(
        f"{lineitem}/scores",
        json=score_payload,
        headers={"Content-Type": "application/vnd.ims.lis.v1.score+json"},
        timeout=5,
    )

    if resp.ok:
        return jsonify({"status": "success", "result": resp.json()})
    else:
        return jsonify({"status": "error", "code": resp.status_code}), 502


# ---------- NRPS：拉取花名册 ----------

@app.route("/api/roster")
def roster():
    """通过 NRPS 端点拉取课程成员列表。"""
    nrps_url = request.args.get("nrps_url")
    if not nrps_url:
        return jsonify({"error": "nrps_url 参数缺失"}), 400

    print(f"[Tool NRPS] 拉取花名册 -> {nrps_url}")
    resp = http.get(nrps_url, timeout=5)
    return jsonify(resp.json())


# ---------- 内容直链（无 LTI 上下文）----------

@app.route("/content/<resource_id>")
def content(resource_id):
    return (
        f"<h1>{resource_id}</h1>"
        "<p>直接访问，无 LTI 上下文。请从 LMS 课程页面点击链接进入。</p>"
    )


if __name__ == "__main__":
    print("=" * 50)
    print("LTI Demo Tool 启动中 (http://localhost:8001)")
    print("=" * 50)
    app.run(port=8001, debug=True, use_reloader=False)
