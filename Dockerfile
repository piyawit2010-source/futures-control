# ใช้ Python เวอร์ชันล่าสุด (เหมาะกับ Cloud Run)
FROM python:3.11-slim

# ตั้ง working directory
WORKDIR /app

# คัดลอกไฟล์ทั้งหมดเข้า container
COPY . /app

# ติดตั้ง dependencies ถ้ามี
RUN pip install --no-cache-dir flask requests python-binance

# ระบุพอร์ตที่ Cloud Run จะใช้
ENV PORT=8080

# รันโปรแกรม
CMD ["python", "main.py"]
