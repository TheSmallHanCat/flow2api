# CLI test for backend image generation API
# Independent script to test the image generation functionality

# Author: DAntyNoel
# Date: 2025-12-18

import os
import json
import re
import base64
import aiohttp # Async test. Need to install
import asyncio

class Flow2APIRequestError(Exception):
    """Custom exception for Flow2API request errors."""
    pass

# --- 配置区域 ---
BASE_URL = os.getenv('GEMINI_FLOW2API_URL', 'http://127.0.0.1:8000')
BACKEND_URL = BASE_URL + "/v1/chat/completions"
API_KEY = os.getenv('GEMINI_FLOW2API_APIKEY', 'Bearer han1234')
MODEL_LANDSCAPE = "gemini-3.0-pro-image-landscape"
MODEL_PORTRAIT = "gemini-3.0-pro-image-portrait"

async def auto_create_token():
    """模拟自动创建Token的逻辑（示例）"""
    from src.services.token_manager import TokenManager

# 修改: 增加 model 参数，默认为 None
async def request_backend_generation(
        prompt: str,
        images: list[bytes] = None,
        model: str = None,
        auto_create_st: bool = False) -> bytes | None:
    """
    请求后端生成图片。
    :param prompt: 提示词
    :param images: 图片二进制列表
    :param model: 指定模型名称 (可选)
    :param auto_create_st: 是否在所有token均失效时自动创建新的Token (可选)
    :return: 成功返回图片bytes，失败返回None
    """
    # 更新token
    images = images or []
    
    # 逻辑: 如果未指定 model，默认使用 Landscape
    use_model = model if model else MODEL_LANDSCAPE

    # 1. 构造 Payload
    if images:
        content_payload = [{"type": "text", "text": prompt}]
        print(f"[Backend] 正在处理 {len(images)} 张图片输入...")
        for img_bytes in images:
            b64_str = base64.b64encode(img_bytes).decode('utf-8')
            content_payload.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}
            })
    else:
        content_payload = prompt

    payload = {
        "model": use_model,  # 使用选定的模型
        "messages": [{"role": "user", "content": content_payload}],
        "stream": True
    }
    
    headers = {
        "Authorization": API_KEY,
        "Content-Type": "application/json"
    }

    image_url = None
    print(f"[Backend] Model: {use_model} | 发起请求: {prompt[:20]}...") 
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(BACKEND_URL, json=payload, headers=headers, timeout=120) as response:
                if response.status != 200:
                    err_text = await response.text()
                    content = response.content
                    print(f"[Backend Error] Status {response.status}: {err_text} {content}")
                    raise Exception(f"API Error: {response.status}: {err_text}")

                async for line in response.content:
                    line_str = line.decode('utf-8').strip()
                    if line_str.startswith('{"error'):
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        msg = delta['reasoning_content']
                        if '401' in msg:
                            msg += '\nAccess Token 已失效，需重新配置。'
                        elif '400' in msg:
                            msg += '\n返回内容被拦截。'
                        elif '403' in msg:
                            msg += '\nreCaptcha 验证失败。'
                        elif '所有Token' in msg:
                            # 没有可用Token，自动获取一个新的token
                            pass
                        
                        print(f"[Backend Error] {chunk}")
                        raise Flow2APIRequestError(msg)

                    if not line_str or not line_str.startswith('data: '):
                        continue
                    
                    data_str = line_str[6:]
                    if data_str == '[DONE]':
                        break
                    
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        
                        # 输出流式内容
                        if "reasoning_content" in delta:
                            print(delta['reasoning_content'], end="", flush=True)

                        # 提取内容中的图片链接
                        if "content" in delta:
                            content_text = delta["content"]
                            img_match = re.search(r'!\[.*?\]\((.*?)\)', content_text)
                            if img_match:
                                image_url = img_match.group(1)
                                print(f"\n[Backend] 捕获图片链接: {image_url}")
                    except json.JSONDecodeError:
                        continue
            
            # 3. 下载生成的图片
            if image_url:
                async with session.get(image_url) as img_resp:
                    if img_resp.status == 200:
                        image_bytes = await img_resp.read()
                        return image_bytes
                    else:
                        print(f"[Backend Error] 图片下载失败: {img_resp.status}")
    except Flow2APIRequestError as e:
        raise e
    except Exception as e:
        print(f"[Backend Exception] Unexpected error: {e}")
        raise e 
        
    return None

if __name__ == '__main__':
    async def main():
        print("=== AI 绘图接口测试 ===")
        user_prompt = input("请输入提示词 (例如 '一只猫'): ").strip()
        if not user_prompt:
            user_prompt = "A cute cat in the garden"
        
        print(f"正在请求: {user_prompt}")
        
        # 这里的 images 传空列表用于测试文生图
        # 如果想测试图生图，你需要手动读取本地文件：
        # with open("output_test.jpg", "rb") as f: img_data = f.read()
        # result = await request_backend_generation(user_prompt, [img_data])
        
        result = await request_backend_generation(user_prompt)
        
        if result:
            filename = "output_test.jpg"
            with open(filename, "wb") as f:
                f.write(result)
            print(f"\n[Success] 图片已保存为 {filename}，大小: {len(result)} bytes")
        else:
            print("\n[Failed] 生成失败")

    # 运行测试
    if os.name == 'nt':  # Windows 兼容性
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())

    
# if __name__ == '__main__':
#     from src.services.browser_captcha_personal import BrowserCaptchaService
#     async def test():
#         service = await BrowserCaptchaService.get_instance()
#         await service.open_login_window()
#         cookies = await service.get_flow_cookies()
#         print(cookies)
#         await asyncio.sleep(60)
    
#     asyncio.run(test())