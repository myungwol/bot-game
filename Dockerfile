# 1. 베이스 이미지 설정 (Python 3.11 환경)
FROM python:3.11-slim

# 2. 작업 디렉토리 설정
WORKDIR /app

# 3. 필요한 라이브러리 목록 복사
COPY requirements.txt .

# 4. 라이브러리 설치 (이 과정은 이미지가 빌드될 때 한 번만 실행됨)
RUN pip install --no-cache-dir -r requirements.txt

# 5. 나머지 모든 소스 코드 복사
COPY . .

# 6. 봇 실행 명령어
CMD ["python", "main.py"]
