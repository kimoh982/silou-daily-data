# SILOU DAILY Weather Data

기상청 단기예보 조회서비스를 GitHub Actions로 주기적으로 수집해 `today.json`으로 저장합니다.

## 1. GitHub Secret 설정

Repository → **Settings → Secrets and variables → Actions → New repository secret**

- Name: `KMA_SERVICE_KEY`
- Secret: 공공데이터포털에서 발급받은 **일반 인증키**

인증키는 저장소 파일에 직접 넣지 않습니다.

## 2. 첫 실행

Repository → **Actions → Update SILOU Daily Weather → Run workflow**

실행 성공 후 저장소 루트의 `today.json`이 실제 기상청 데이터로 갱신됩니다.

## 3. 자동 갱신

`.github/workflows/update-weather.yml`은 매시간 자동 실행되도록 설정되어 있습니다.
GitHub Actions의 cron은 UTC 기준이며 현재 설정 `22 * * * *`은 한국시간으로 매시 22분입니다.

## 4. 식스샵에서 읽을 데이터

식스샵 Blockmaker는 다음 형태의 raw GitHub URL을 읽습니다.

`https://raw.githubusercontent.com/<OWNER>/silou-daily-data/main/today.json`

`<OWNER>`만 본인 GitHub 사용자명으로 변경합니다.

## 현재 범위

- 서울 / 부산 / 대구 / 인천 / 광주 / 대전 / 울산 / 제주
- 현재기온
- 계산 체감온도(표시용 근사값)
- 최저/최고기온
- 습도
- 풍속
- 강수확률
- 현재 강수형태 / 1시간 강수량
- 날씨 상태

미세먼지와 UV는 날씨 파이프라인 확인 후 별도 공식 데이터 소스로 연결합니다.
