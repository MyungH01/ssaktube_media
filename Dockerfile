# Python 버전
FROM python:3.10

# 컨테이너 내의 작업 디렉토리 설정
WORKDIR /usr/src/app

# requirements.txt 복사
COPY requirements.txt ./

# requirements.txt에 명시된 필요한 패키지 설치
RUN pip install --no-cache-dir -r requirements.txt

# 현재 디렉토리의 내용을 컨테이너의 /usr/src/app에 복사
COPY . .

# 외부에 포트 80을 공개
EXPOSE 80

# 실행
CMD ["uvicorn", "channel:app", "--host", "0.0.0.0", "--port", "80"]
