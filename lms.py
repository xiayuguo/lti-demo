"""
lms.py - 最小化 LTI 1.3 Platform（模拟 LMS）
运行端口：8000

注意：不要把此文件命名为 platform.py。
platform 是 Python 标准库模块名，Flask/Werkzeug 内部会 import platform，
在同目录下会导致循环导入：
  ImportError: cannot import name 'Flask' from partially initialized module 'flask'

启动前确保已执行 keygen.py，生成 platform_private.pem 和 platform_jwks.json。

LTI 1.3 流程中 Platform 承担的角色：
  1. 发起 OIDC 第三方登录请求 -> Tool 的 /lti/login
  3. 接收 Tool 重定向，生成并签名 id_token JWT -> POST 到 Tool 的 /lti/launch
  *. 暴露 JWKS 端点供 Tool 验证签名
  *. 提供 AGS 端点接收 Tool 回传的成绩
"""

import json
import time
import uuid
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from flask import Flask, jsonify, redirect, render_template_string, request

app = Flask(__name__)

# ---------- 加载密钥 ----------

with open("platform_private.pem", "rb") as f:
    PLATFORM_PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)

with open("platform_jwks.json") as f:
    PLATFORM_JWKS = json.load(f)

# ---------- 配置常量 ----------

PLATFORM_ISS = "http://localhost:8000"
PLATFORM_KEY_ID = "platform-key-1"

# 已注册的 Tool 列表（生产环境存数据库）
REGISTERED_TOOLS = {
    "tool-client-001": {
        "name": "Demo Coding Tool",
        "login_url": "http://localhost:8001/lti/login",
        "redirect_uri": "http://localhost:8001/lti/launch",
        "deployment_id": "deployment-001",
    }
}

# 模拟成绩册（内存，重启丢失）
GRADEBOOK: dict[str, dict] = {}

# ---------- 课程主页 ----------

COURSE_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>Demo LMS - CS101</title>
  <style>
    body { font-family: sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; }
    h1 { color: #07c160; }
    .resource { border: 1px solid #ddd; border-radius: 8px; padding: 14px 18px; margin: 12px 0; }
    a { color: #07c160; text-decoration: none; font-weight: bold; }
    a:hover { text-decoration: underline; }
    .badge { background: #07c160; color: white; border-radius: 4px;
             padding: 2px 8px; font-size: 12px; margin-left: 8px; }
  </style>
</head>
<body>
  <h1>Demo LMS (Platform)</h1>
  <h2>CS101 - Introduction to Computer Science</h2>
  <p>以下是本课程的学习资源，点击后将通过 LTI 1.3 协议跳转到外部工具：</p>

  <div class="resource">
    <a href="/launch?resource_id=week3-coding&client_id=tool-client-001">
      Week 3 - 编程练习
    </a>
    <span class="badge">LTI Tool</span>
    <p>角色：以 <strong>学生（Learner）</strong> 身份启动</p>
  </div>

  <div class="resource">
    <a href="/launch?resource_id=week3-coding&client_id=tool-client-001&role=Instructor">
      Week 3 - 编程练习（教师视图）
    </a>
    <span class="badge">LTI Tool</span>
    <p>角色：以 <strong>教师（Instructor）</strong> 身份启动</p>
  </div>

  <hr>
  <h3>成绩册（AGS 接收记录）</h3>
  <pre>{{ gradebook }}</pre>
</body>
</html>
"""


@app.route("/")
def index():
    gradebook_text = (
        json.dumps(GRADEBOOK, indent=2, ensure_ascii=False) if GRADEBOOK else "（暂无成绩）"
    )
    return render_template_string(COURSE_PAGE, gradebook=gradebook_text)


# ---------- Step 1: Platform 发起 LTI Launch ----------

@app.route("/launch")
def launch():
    """
    Step 1: 用户点击课程资源链接，Platform 向 Tool 发起 OIDC 第三方登录请求。

    这一步相当于告诉 Tool："有用户要来了，准备好你的 OIDC 流程。"
    """
    client_id = request.args.get("client_id")
    resource_id = request.args.get("resource_id", "default-resource")
    role = request.args.get("role", "Learner")  # 仅 demo 用，实际由 LMS 用户角色决定

    tool = REGISTERED_TOOLS.get(client_id)
    if not tool:
        return f"未知工具 client_id={client_id}", 400

    # OIDC Third-Party Initiated Login 参数
    params = {
        "iss": PLATFORM_ISS,
        "login_hint": f"user-{role.lower()}-001",   # 实际 LMS 中是真实用户 ID
        "target_link_uri": f"http://localhost:8001/content/{resource_id}",
        "client_id": client_id,
        "lti_message_hint": f"{resource_id}|{role}",  # 透传给 auth 端点的上下文
    }

    print(f"[Platform] Step 1: 发起登录请求 -> {tool['login_url']}")
    print(f"           参数: {params}")

    return redirect(f"{tool['login_url']}?{urlencode(params)}")


# ---------- Step 3: Platform 授权端点，签发 JWT ----------

AUTH_FORM = """
<!DOCTYPE html>
<html>
<body onload="document.getElementById('lti_form').submit()">
  <form id="lti_form" method="POST" action="{{ redirect_uri }}">
    <input type="hidden" name="id_token" value="{{ id_token }}">
    <input type="hidden" name="state" value="{{ state }}">
  </form>
  <p>正在跳转到工具，请稍候...</p>
</body>
</html>
"""


@app.route("/lti/auth")
def auth():
    """
    Step 3: Tool 重定向回 Platform 的授权端点。
    Platform 验证参数后生成 id_token（JWT），通过浏览器自动 POST 发给 Tool。

    实际 LMS 在此处还会验证用户会话（确认用户已登录）。
    """
    client_id = request.args.get("client_id")
    redirect_uri = request.args.get("redirect_uri")
    nonce = request.args.get("nonce")
    state = request.args.get("state")
    login_hint = request.args.get("login_hint", "anonymous")
    lti_message_hint = request.args.get("lti_message_hint", "default-resource|Learner")

    # 基本校验
    tool = REGISTERED_TOOLS.get(client_id)
    if not tool:
        return f"未知 client_id: {client_id}", 400
    if redirect_uri != tool["redirect_uri"]:
        return f"redirect_uri 不匹配", 400

    # 解析透传的上下文
    parts = lti_message_hint.split("|", 1)
    resource_id = parts[0]
    role_name = parts[1] if len(parts) > 1 else "Learner"

    roles = {
        "Learner": [
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
        ],
        "Instructor": [
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
        ],
    }
    user_roles = roles.get(role_name, roles["Learner"])

    user_info = {
        "Learner":    {"name": "Alice Student", "email": "alice@example.edu"},
        "Instructor": {"name": "Bob Teacher",   "email": "bob@example.edu"},
    }.get(role_name, {"name": "Unknown", "email": ""})

    now = int(time.time())

    # 构造 LTI 1.3 JWT payload
    payload = {
        # --- 标准 OIDC claims ---
        "iss": PLATFORM_ISS,
        "sub": login_hint,          # 用户唯一标识
        "aud": client_id,           # 目标 Tool 的 client_id
        "iat": now,
        "exp": now + 300,           # 5 分钟内有效
        "nonce": nonce,             # Tool 生成，回传用于防重放

        # --- LTI 核心 claims ---
        "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiResourceLinkRequest",
        "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id": tool["deployment_id"],
        "https://purl.imsglobal.org/spec/lti/claim/target_link_uri": (
            f"http://localhost:8001/content/{resource_id}"
        ),

        # 资源链接信息
        "https://purl.imsglobal.org/spec/lti/claim/resource_link": {
            "id": resource_id,
            "title": "Week 3 Coding Exercise",
            "description": "Complete the linked coding exercise.",
        },

        # 用户角色（URN 格式）
        "https://purl.imsglobal.org/spec/lti/claim/roles": user_roles,

        # 课程上下文
        "https://purl.imsglobal.org/spec/lti/claim/context": {
            "id": "course-cs101-2026",
            "label": "CS101",
            "title": "Introduction to Computer Science",
            "type": ["http://purl.imsglobal.org/vocab/lis/v2/course#CourseOffering"],
        },

        # 用户基本信息
        "name":        user_info["name"],
        "email":       user_info["email"],
        "given_name":  user_info["name"].split()[0],
        "family_name": user_info["name"].split()[-1],

        # --- LTI Advantage: AGS 端点声明 ---
        # Tool 通过此信息知道去哪里提交成绩
        "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint": {
            "scope": [
                "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem",
                "https://purl.imsglobal.org/spec/lti-ags/scope/score",
            ],
            "lineitems": "http://localhost:8000/api/ags/cs101/lineitems",
            "lineitem":  f"http://localhost:8000/api/ags/cs101/lineitems/{resource_id}",
        },

        # --- LTI Advantage: NRPS 端点声明 ---
        "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice": {
            "context_memberships_url": "http://localhost:8000/api/nrps/cs101/memberships",
            "service_versions": ["2.0"],
        },
    }

    # 用 Platform 私钥签名，kid 对应 JWKS 中的公钥
    id_token = jwt.encode(
        payload,
        PLATFORM_PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": PLATFORM_KEY_ID},
    )

    print(f"[Platform] Step 3: 生成 id_token，POST -> {redirect_uri}")

    # 通过浏览器自动提交表单将 id_token 发给 Tool（不能用 redirect，必须 POST）
    return render_template_string(
        AUTH_FORM, redirect_uri=redirect_uri, id_token=id_token, state=state
    )


# ---------- JWKS 端点 ----------

@app.route("/.well-known/jwks.json")
def jwks():
    """
    Platform 的公钥集合端点。
    Tool 在验证 id_token 签名时会来这里拉取公钥。
    """
    return jsonify(PLATFORM_JWKS)


# ---------- AGS 端点：接收 Tool 回传成绩 ----------

@app.route("/api/ags/<course_id>/lineitems/<resource_id>/scores", methods=["POST"])
def receive_score(course_id, resource_id):
    """
    AGS Score 端点。
    生产环境中需验证 Bearer token（OAuth2 Client Credentials）。
    本 demo 跳过 token 验证，直接写入内存成绩册。
    """
    # Content-Type: application/vnd.ims.lis.v1.score+json
    score_data = request.json
    key = f"{course_id}/{resource_id}/{score_data.get('userId', 'unknown')}"
    GRADEBOOK[key] = {
        "scoreGiven":       score_data.get("scoreGiven"),
        "scoreMaximum":     score_data.get("scoreMaximum"),
        "activityProgress": score_data.get("activityProgress"),
        "gradingProgress":  score_data.get("gradingProgress"),
        "timestamp":        score_data.get("timestamp"),
    }
    print(f"[Platform AGS] 收到成绩: {key} -> {score_data.get('scoreGiven')}/{score_data.get('scoreMaximum')}")
    return jsonify({"resultUrl": f"http://localhost:8000/api/ags/{key}/result"}), 200


# ---------- NRPS 端点：提供花名册 ----------

@app.route("/api/nrps/<course_id>/memberships")
def memberships(course_id):
    """
    NRPS 端点，返回课程成员列表。
    生产环境同样需要 Bearer token 验证。
    """
    members = [
        {
            "status": "Active",
            "name": "Alice Student",
            "email": "alice@example.edu",
            "user_id": "user-learner-001",
            "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"],
        },
        {
            "status": "Active",
            "name": "Bob Teacher",
            "email": "bob@example.edu",
            "user_id": "user-instructor-001",
            "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"],
        },
    ]
    return jsonify({
        "id": f"http://localhost:8000/api/nrps/{course_id}/memberships",
        "context": {"id": course_id, "title": "Introduction to Computer Science"},
        "members": members,
    })


if __name__ == "__main__":
    print("=" * 50)
    print("LTI Demo Platform 启动中 (http://localhost:8000)")
    print("=" * 50)
    app.run(port=8000, debug=True, use_reloader=False)
