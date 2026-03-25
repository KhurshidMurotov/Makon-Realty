import asyncio
import asyncpg

async def main():
    # Тот самый URL, который мы проверяем
    url = "postgresql://postgres:1234@127.0.0.1:5432/real_estate_db"
    
    print(f"Попытка подключения к {url}...")
    try:
        # Пробуем просто подключиться
        conn = await asyncpg.connect(url)
        print("✅ УРА! Соединение установлено успешно.")
        
        # Проверим, видит ли он нашу базу
        version = await conn.fetchval("SELECT version();")
        print(f"📦 База данных отвечает: {version[:30]}...")
        
        await conn.close()
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        print("\nПроверь:")
        print("1. Запущен ли PostgreSQL в 'Службах'?")
        print("2. Точно ли пароль '1234'?")

if __name__ == "__main__":
    asyncio.run(main())