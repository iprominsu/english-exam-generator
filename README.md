# English Exam Generator

영어 단어장을 등록하고 시험지와 정답지 PDF를 만드는 웹 앱입니다. 웹 서비스는 문제 생성·편집·다운로드를 담당합니다. 작업자는 업로드한 단어장의 원문 판독과 학습자료 생성을 처리합니다. 두 서비스는 같은 소스와 의존성을 사용합니다.

## 로컬 웹 실행

저장소 루트에서 Docker 명령을 실행합니다. 첫 빌드에는 인터넷 연결이 필요합니다.

```sh
docker build --target web -t english-exam-generator:web .
docker run --rm -p 127.0.0.1:8080:8080 english-exam-generator:web
```

[로컬 웹](http://127.0.0.1:8080/)과 [상태 확인](http://127.0.0.1:8080/healthz)을 열 수 있습니다. 상태 확인 응답은 웹 프로세스가 실행 중이면 `{"ok":true}`입니다. 단어장 조회와 시험지 저장에는 별도로 Google Drive·Sheets 인증이 필요합니다.

## 시험지 생성 테스트

개발 환경에서 저장소 루트의 `python -m unittest discover -s tests`를 실행합니다. 테스트에 포함된 예시 단어장으로 객관식·뜻 쓰기 시험지와 정답, 원문 보존을 확인합니다.

## 실행 환경 설정

환경 변수는 컨테이너를 실행할 때 전달합니다. 웹과 작업자를 외부에 배포할 때는 실행 환경의 비밀 저장소에서 인증 값을 주입합니다.

| 기능 | 설정 |
| --- | --- |
| 웹 접근 | `APP_BASIC_USER`, `APP_BASIC_PASS`를 지정합니다. Cloud Run에서 웹을 실행하려면 비밀번호가 필요합니다. |
| Drive·Sheets | 운영 계정에서 Drive·Sheets API를 활성화하고 `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`을 제공합니다. 소유자 확인이 필요한 작업에는 `APPROVED_DRIVE_EMAIL`을 설정합니다. |
| AI 원문 판독·학습자료 | 이 버전은 Codex CLI 구독 인증을 사용합니다. 개인 계정 경로는 `APPROVED_GPT_EMAIL` 및 새 토큰 버전을 보관할 수 있는 `CODEX_AUTH_SECRET`을 요구합니다. |
| 작업 상태와 호출 | Firestore 데이터베이스, Cloud Tasks 큐, 작업자 HTTPS 주소와 호출 서비스 계정을 준비합니다. 복구 요청은 Cloud Scheduler가 보냅니다. 작업 API는 Google ID 토큰의 대상 주소와 호출자 계정을 검사합니다. |

## 작업자 실행

```sh
docker build --target worker -t english-exam-generator:worker .
docker run --rm --env-file .env.worker -p 127.0.0.1:8081:8080 english-exam-generator:worker
```

`.env.worker`는 관리자가 로컬에 준비하는 실행 설정입니다. `IMPORT_ENGINE_ENABLED=true`를 지정하고, 승인된 인증 방식에 맞게 `IMPORT_AUTH_COORDINATOR_FACTORY`와 인증 확인 변수를 설정합니다. 웹에서 작업을 등록하려면 웹에도 해당 엔진 설정을 전달합니다.

- `IMPORT_GCP_PROJECT`와 `IMPORT_APPROVED_GCP_PROJECT`에 같은 프로젝트 ID를 지정합니다. 리전의 실행 값과 승인 값은 각각 `IMPORT_GCP_REGION`, `IMPORT_APPROVED_GCP_REGION`입니다.
- `IMPORT_FIRESTORE_DATABASE`, `IMPORT_TASK_QUEUE`, `IMPORT_WORKER_URL`, `IMPORT_TASK_SERVICE_ACCOUNT`를 확인합니다. 별도 스케줄러 계정을 사용하면 `IMPORT_SCHEDULER_SERVICE_ACCOUNT`를 지정합니다.
- 전체 유효성 검사와 인증 방식별 조건은 `feature_word_meaning/import_cloud.py`의 `CloudSettings.validate` 및 `_validate_auth_factory`에 있습니다. 실행 주체에는 Firestore·Cloud Tasks와 선택한 인증 저장소에 필요한 Google Cloud 권한을 부여합니다.

웹 진입점은 `app.main:app`, 작업자 진입점은 `feature_word_meaning.import_cloud:create_cloud_worker`입니다. 스캔 PDF의 OCR에는 컨테이너에 설치한 한국어·영어 Tesseract를 사용합니다. 새 계정으로 연동을 확인할 때는 검수용 단어장으로 등록, 작업 완료, 시험지 생성, PDF 다운로드까지 시험합니다.

## 코드 이용과 상업화

누구나 비영리 목적으로 코드를 실행·수정하고 원본이나 수정본을 무료로 배포할 수 있습니다. 배포할 때는 이용조건과 제3자 고지를 함께 전달합니다. 판매·유료 서비스·광고 수익 등 상업적 이용에는 권리자의 사전 개별 승인이 필요합니다. 승인받은 개발자는 수정 사항을 보고할 의무 없이 자기 버전을 배포·판매하거나 유료 서비스를 운영할 수 있습니다. [자체 코드 이용조건](LICENSE)

## 포함된 외부 구성 요소

- Noto Sans KR 글꼴에는 SIL Open Font License 1.1이 적용됩니다. [라이선스](static/fonts/OFL.txt) · [저작권 고지](static/fonts/FONT-NOTICES.txt)
- PDF.js에는 Apache License 2.0이 적용됩니다. [라이선스](static/vendor/pdfjs/LICENSE)
