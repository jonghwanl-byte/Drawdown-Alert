# 고점 대비 하락률 텔레그램 리포트

종목별로 **최근 20일 / 60일 / 200일 종가 최고점 대비 현재 몇 % 빠졌는지**를 텔레그램으로 보내줍니다.
괄호 안 숫자는 같은 기간 **이동평균선 대비 현재가 위치**(+면 위, -면 아래)입니다.

## 대상 종목

| 티커 | 이름 |
|---|---|
| XLE | 미국 에너지 |
| SOXQ | 필라델피아 반도체 |
| 069500.KS | 한국 주식 |
| 283580.KS | 중국 주식 |
| 241180.KS | 일본 주식 |
| 453810.KS | 인도 주식 |
| 385560.KS | 채권 30년 |

종목 변경은 `signal_report.py` 위쪽 `TICKERS` 를 수정하세요.

## 메시지 예시

```
✅ 한국 주식 (069500.KS) 11-07 기준
 20일   -2.1%  (  +0.5%)
 60일   -4.3%  (  +2.8%)
200일   -6.0%  ( +10.2%)
```

- ⚠️ : 20일 -5% / 60일 -8% / 200일 -12% 중 하나라도 넘게 빠짐 (`ALERT_DD` 에서 조정)
- ✅ : 기준 이내

## 파일 구성

```
signal_report.py                  # 계산 + 텔레그램 발송
requirements.txt
.github/workflows/daily_report.yml  # 수동/API 호출용 실행 workflow
.github/workflows/keep_alive.yml    # 비활성화 방지(매월 1·16일 자동 커밋)
```

## 설정 순서

### 1) 텔레그램
1. 텔레그램에서 `@BotFather` → `/newbot` → **Bot Token** 받기
2. 만든 봇에게 아무 메시지나 보내기 (이걸 안 하면 `PEER_ID_INVALID` 오류가 납니다)
3. 브라우저에서 `https://api.telegram.org/bot<TOKEN>/getUpdates` 열어 `"chat":{"id":...}` 숫자가 **Chat ID**

### 2) GitHub 저장소
1. 새 저장소를 만들고 이 폴더의 파일을 모두 올리기
2. **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. **Settings → Actions → General → Workflow permissions → Read and write permissions** 선택 (keep-alive 커밋용)
4. **Actions → Daily Drawdown Report → Run workflow** 로 수동 테스트

### 3) cron-job.org 로 매일 실행
1. GitHub **Settings → Developer settings → Personal access tokens → Fine-grained tokens**
   - 이 저장소만 선택, 권한 **Actions: Read and write**
2. cron-job.org 에서 새 Cronjob 생성
   - URL: `https://api.github.com/repos/<계정>/<저장소>/actions/workflows/daily_report.yml/dispatches`
   - Request method: `POST`
   - Request body: `{"ref":"main"}`
   - Headers:
     - `Authorization: Bearer <GitHub 토큰>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - 시간: 원하는 시각, 타임존 `Asia/Seoul` (예: 월~토 06:30)
3. 성공하면 응답 코드 **204** 가 나옵니다.

## 참고

- 가격은 yfinance 배당 조정 종가이며, 한국 ETF는 한국 장 마감 기준, 미국 ETF는 직전 미국 종가 기준입니다. 종목별 기준일이 메시지에 표시됩니다.
- 하락률 = 현재 종가 / 최근 N일 종가 최고값 − 1
- 일부 종목 조회에 실패하면 나머지만 보내고 실패 종목을 메시지에 표시합니다.
- `cron-job.org` 로만 실행하므로 GitHub `schedule` 은 쓰지 않습니다. keep-alive 는 저장소 비활성화 방지용입니다.
- 투자 판단의 참고용 정보이며 매매 권유가 아닙니다.

## 안전장치 (데이터 조회 실패·구데이터 대응)

- 야후 3경로(download / history / chart API)를 모두 시도해 **가장 최신 날짜**를 채택합니다.
- 장 마감 전에 받은 오늘자 미완성 봉은 제거합니다.
- 종목별로 기대하는 마지막 거래일(미국 동부 16:30 / 한국 16:00 이후 기준)보다 데이터가 오래되면 **최대 3회(90초 간격) 재조회**하고, 끝까지 안 맞으면 `⚠️ 데이터 최신 아님` 경고를 붙여 발송합니다. 휴장일이면 정상 경고입니다.
- 상장 20일 미만이면 조회 실패로 빼고, 20일 이상이면 있는 구간만 계산해 나머지는 `N/A`로 표시합니다.
- 실행 중 오류가 나면 텔레그램으로 `❌ 실행 오류`를 보내고 workflow 는 실패(빨간 X)로 끝납니다.
- 로컬 테스트: `python signal_report.py --dry-run` (발송 없이 화면 출력)
