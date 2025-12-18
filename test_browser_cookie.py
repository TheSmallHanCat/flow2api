# CLI test for backend admin login and browser cookie retrieval
# Independent script to test the browser cookie retrieval functionality

# Author: DAntyNoel
# Date: 2025-12-18

"""
测试浏览器 Cookie 获取功能
"""
import requests # 需要额外安装 requests 库
import os

# 配置
BASE_URL = os.getenv('GEMINI_FLOW2API_URL', 'http://127.0.0.1:8000')
ADMIN_USERNAME = "admin"  
ADMIN_PASSWORD = "admin"  


def login():
    """登录获取 session token"""
    url = f"{BASE_URL}/api/admin/login"
    payload = {
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD
    }
    
    print(f"正在登录到 {url}...")
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            token = data.get("token")
            print(f"✅ 登录成功！Session Token: {token[:30]}...")
            return token
        else:
            print(f"❌ 登录失败: {data}")
            return None
    else:
        print(f"❌ HTTP 错误 {response.status_code}: {response.text}")
        return None


def get_flow_cookies(token):
    """获取 Google Flow cookies"""
    url = f"{BASE_URL}/api/browser/get-flow-cookies"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    print(f"\n正在获取 cookies...")
    response = requests.post(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            print(f"✅ 成功获取 cookies!")
            cookies = data.get("cookies", {})
            simple_cookies = cookies.get("simple", {})
            
            print(f"\n📋 简化版 Cookies ({len(simple_cookies)} 个):")
            for name, value in simple_cookies.items():
                # 截断显示长值
                display_value = value[:50] + "..." if len(value) > 50 else value
                print(f"  - {name}: {display_value}")
            
            # 检查是否有 session token
            session_keys = [
                '__Secure-next-auth.session-token',
                'next-auth.session-token',
                '__Secure-session-token',
                'session-token'
            ]
            
            found_st = None
            for key in session_keys:
                if key in simple_cookies:
                    found_st = key
                    break
            
            if found_st:
                print(f"\n✅ 找到 Session Token: {found_st}")
                print(f"   值: {simple_cookies[found_st][:50]}...")
            else:
                print(f"\n⚠️  未找到 Session Token")
                print(f"   可用的 cookies: {list(simple_cookies.keys())}")
            
            return data
        else:
            print(f"❌ 获取失败: {data.get('message')}")
            return None
    else:
        print(f"❌ HTTP 错误 {response.status_code}: {response.text}")
        return None


def auto_add_token(token):
    """自动从浏览器添加 Token"""
    url = f"{BASE_URL}/api/browser/auto-add-token"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    print(f"\n正在自动添加 Token...")
    response = requests.post(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            print(f"✅ 成功添加 Token!")
            token_info = data.get("token", {})
            print(f"   ID: {token_info.get('id')}")
            print(f"   Email: {token_info.get('email')}")
            print(f"   Name: {token_info.get('name')}")
            print(f"   Credits: {token_info.get('credits')}")
            print(f"   Active: {token_info.get('is_active')}")
            return data
        else:
            print(f"❌ 添加失败: {data.get('message')}")
            if 'traceback' in data:
                print(f"\n错误详情:\n{data['traceback']}")
            return None
    else:
        print(f"❌ HTTP 错误 {response.status_code}: {response.text}")
        return None


def main():
    print("=" * 60)
    print("浏览器 Cookie 获取功能测试")
    print("=" * 60)
    
    # 1. 登录
    session_token = login()
    if not session_token:
        print("\n❌ 登录失败，无法继续测试")
        return
    
    # 2. 获取 cookies
    print("\n" + "=" * 60)
    print("测试 1: 获取 Google Flow Cookies")
    print("=" * 60)
    get_flow_cookies(session_token)
    
    # 3. 自动添加 Token
    print("\n" + "=" * 60)
    print("测试 2: 自动添加 Token")
    print("=" * 60)
    
    choice = input("\n是否尝试自动添加 Token? (y/n): ").strip().lower()
    if choice == 'y':
        auto_add_token(session_token)
    else:
        print("跳过自动添加 Token")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
