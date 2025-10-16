import os
import httpx
import asyncio

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

async def test():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                "https://api.deepseek.ai/v1/text/generate",
                json={"prompt":"Привет", "max_tokens":10},
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
            )
            print(resp.json())
        except Exception as e:
            print("Ошибка запроса:", e)

asyncio.run(test())
