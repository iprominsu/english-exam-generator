# English Exam Generator

영어 단어장(PDF·Excel)을 등록해 시험지와 정답지 PDF를 제작하는 웹 애플리케이션입니다. 하나의 소스와 의존성을 공유하는 두 서비스로 구성되어 있으며, **웹 서비스**는 시험지 출제·편집·다운로드를 맡고 **작업자(Worker)**는 업로드된 단어장의 원문 판독과 보조 학습자료 생성을 비동기로 처리합니다.

## 서비스 구성

- **웹 서비스** (`app.main:app`): 단어장 선택, 유형별 문항 생성·편집, 문제지·정답지 PDF 렌더링 및 다운로드를 제공합니다.
- **등록 작업자** (`feature_word_meaning.import_cloud:create_cloud_worker`): 단어장 원문 판독·검수와 예문·정의·지문 등 보조 학습자료 생성을 수행합니다. 스캔 PDF 판독에는 컨테이너에 내장된 한국어·영어 Tesseract OCR을 사용합니다.

## 빠른 시작

### 1. 로컬 웹 실행

저장소 루트에서 아래 Docker 명령을 실행합니다. 최초 빌드 시에는 패키지 설치를 위해 인터넷 연결이 필요합니다.

```sh
docker build --target web -t english-exam-generator:web .
docker run --rm -p 127.0.0.1:8080:8080 english-exam-generator:web
```

실행 후 [로컬 웹 화면](http://127.0.0.1:8080/)과 [상태 확인 경로](http://127.0.0.1:8080/healthz)에 접속할 수 있으며, 웹 프로세스가 정상 동작 중이면 상태 확인 경로에서 `{"ok":true}`를 반환합니다. 실제 단어장 조회와 시험지 저장 기능을 사용하려면 아래의 Google Drive·Sheets 인증 환경 변수를 함께 설정해야 합니다.

### 2. 단위 테스트 실행

개발 환경의 저장소 루트에서 다음 명령으로 테스트를 실행할 수 있습니다.

```sh
python -m unittest discover -s tests
```

내장된 예시 단어장을 바탕으로 어휘·단어–뜻·뜻 쓰기 시험지와 정답 생성, 원문 보존 규칙, 주요 회귀 시나리오를 검증합니다.

## 실행 환경 설정

환경 변수는 컨테이너 실행 시점에 전달하며, 외부 환경에 배포할 때는 플랫폼의 비밀 저장소(Secret Manager 등)를 통해 인증 정보를 주입합니다.

| 구분 | 설정 방법 |
| --- | --- |
| **웹 접근 보호** | `APP_BASIC_USER`, `APP_BASIC_PASS`를 설정합니다. Cloud Run 환경에서 웹 서비스를 실행할 때는 `APP_BASIC_PASS` 설정이 필수입니다. |
| **Google Drive·Sheets** | 운영 계정에서 Drive·Sheets API를 활성화한 뒤 `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`을 전달합니다. 계정 소유자 검증이 필요한 경로를 위해 `APPROVED_DRIVE_EMAIL`도 함께 지정합니다. |
| **AI 원문 판독·학습자료** | Codex CLI 구독 인증을 사용합니다. 개인 계정 경로를 사용할 때는 승인 계정 이메일(`APPROVED_GPT_EMAIL`)과 갱신된 토큰 버전을 저장할 Secret 경로(`CODEX_AUTH_SECRET`)가 필요합니다. |
| **비동기 작업 및 호출 인증** | Firestore 데이터베이스, Cloud Tasks 큐, 작업자 HTTPS 주소, 호출용 서비스 계정을 준비합니다. 중단 작업 복구 요청은 Cloud Scheduler가 주기적으로 호출하며, 작업자 API는 Google ID 토큰의 대상 주소(Audience)와 호출자 서비스 계정을 검증합니다. |

## 작업자(Worker) 실행 및 연동 점검

비동기 단어장 등록·학습자료 생성을 담당하는 작업자 컨테이너는 다음과 같이 빌드하고 실행합니다.

```sh
docker build --target worker -t english-exam-generator:worker .
docker run --rm --env-file .env.worker -p 127.0.0.1:8081:8080 english-exam-generator:worker
```

`.env.worker`는 관리자가 로컬에서 준비하는 환경 설정 파일입니다. `IMPORT_ENGINE_ENABLED=true`로 엔진을 활성화하고, 사용할 인증 방식에 맞춰 `IMPORT_AUTH_COORDINATOR_FACTORY`와 관련 인증 확인 변수를 지정합니다. 웹 서비스에서도 작업자 큐에 등록 작업을 접수하려면 동일한 엔진 설정을 웹 컨테이너에 함께 전달해야 합니다.

- **프로젝트·리전 일치 확인**: `IMPORT_GCP_PROJECT`와 `IMPORT_APPROVED_GCP_PROJECT`에 동일한 Google Cloud 프로젝트 ID를 지정합니다. 리전 역시 실행 값(`IMPORT_GCP_REGION`)과 승인 값(`IMPORT_APPROVED_GCP_REGION`)이 일치해야 합니다.
- **큐·저장소·계정 지정**: `IMPORT_FIRESTORE_DATABASE`, `IMPORT_TASK_QUEUE`, `IMPORT_WORKER_URL`, `IMPORT_TASK_SERVICE_ACCOUNT`를 설정합니다. 스케줄러 호출에 별도 서비스 계정을 사용하는 경우 `IMPORT_SCHEDULER_SERVICE_ACCOUNT`를 추가로 지정합니다.
- **권한 및 상세 검증 규칙**: 실행 주체에는 Firestore, Cloud Tasks 및 선택한 인증 저장소 접근에 필요한 Google Cloud IAM 권한을 부여합니다. 전체 설정 검증 규칙과 인증 방식별 세부 조건은 `feature_word_meaning/import_cloud.py`의 `CloudSettings.validate` 및 `_validate_auth_factory`에서 확인할 수 있습니다.

새 계정이나 새 환경에 연결한 뒤에는 검수용 단어장으로 **단어장 등록 → 작업 완료 확인 → 시험지 생성 → PDF 다운로드** 순서까지 정상 동작하는지 점검하는 것을 권장합니다.

## 코드 이용 및 상업화 안내

누구나 비영리 목적으로 이 코드를 복제·실행·수정할 수 있으며, 원본이나 수정본을 무료로 재배포할 수 있습니다. 배포 시에는 본 저장소의 이용조건과 제3자 오픈소스 고지를 함께 포함해야 합니다.

소프트웨어나 수정본의 판매, 유료 서비스 운영, 광고 수익 기반 서비스 등 상업적 이용에는 권리자의 사전 개별 승인이 필요합니다. 개별 승인을 받은 개발자는 수정 내역을 별도로 보고할 의무 없이 자체 버전을 배포·판매하거나 유료 서비스를 운영할 수 있습니다. 자세한 내용은 [자체 코드 이용조건](LICENSE)을 참고하세요.

## 포함된 외부 구성 요소

- **Noto Sans KR 글꼴**: SIL Open Font License 1.1 적용 ([라이선스](static/fonts/OFL.txt) · [저작권 고지](static/fonts/FONT-NOTICES.txt))
- **PDF.js**: Apache License 2.0 적용 ([라이선스](static/vendor/pdfjs/LICENSE))
