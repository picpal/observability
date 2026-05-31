# 폐쇄망 반입 보안심의 근거 자료 — Monitoring VM (Prometheus + Grafana)

본 문서는 message-gate 관측 스택을 **온프렘 폐쇄망에 반입·설치**하기 위한 보안성 심의 제출 근거다.
신규 보안통제를 정의하는 문서가 아니라, **현재 구성에 이미 적용된 통제와 잔여위험을 사실 그대로 정리**한
것이다. 각 항목은 레포의 실제 설정 파일을 근거로 추적 가능하다.

- 설치 절차: [`install-airgap.md`](install-airgap.md)
- 앱/메트릭 보안 설계: [`setup.md`](setup.md) (특히 §7 보안 체크리스트)
- 운영 스택 정의: `docker-compose.prod.yml`
- scrape 설정 템플릿: `prometheus/prometheus.prod.example.yml`

> **심의위원 안내**: §3 네트워크 연결 명세가 방화벽 신청서 근거다. §10 잔여위험은 본 구성이
> *수용을 요청*하는 항목으로, TLS·취약점 스캔·감사로그는 현재 미적용임을 명시한다 (은폐 없음).

---

## 1. 심의 범위와 시스템 개요

| 항목 | 내용 |
|---|---|
| 대상 | 폐쇄망 Monitoring VM 1대 (Prometheus + Grafana, docker compose) |
| 모니터링 대상 | message-gate WAS 2대 (`mg-was-1`, `mg-was-2`) — 동일 내부망 |
| 수집 방식 | **Pull(scrape)** — Monitoring VM 이 각 WAS 의 메트릭 엔드포인트를 주기 조회 |
| 인터넷 연결 | **없음** (반입 후 완전 격리). 외부 아웃바운드 시도는 §5 로 비활성화 |
| 반입 경로 | 인터넷 준비 호스트에서 번들 생성 → 승인 매체 → 폐쇄망 |

```
[동일 내부망]
 mg-was-1/2.internal
   ├ WAS actuator :9090 (평문)     ──┐
   └ nginx exporter :9113 (평문)   ──┤ pull  (출발지=Monitoring VM 한정 ACL)
                                      ▼
                          [Monitoring VM] docker compose
                           ├ prometheus :9090 (127.0.0.1 바인딩, 외부 비노출)
                           └ grafana    :3000 (내부망 노출, 운영자 접근)
```

---

## 2. 반입 자산 목록 (공급망)

| 자산 | 버전(고정) | 출처(공식) | 용도 |
|---|---|---|---|
| `prom/prometheus` | `v2.54.1` | Docker Hub 공식 | 메트릭 수집/저장 |
| `grafana/grafana` | `11.2.2` | Docker Hub 공식 | 시각화 |
| `nginx/nginx-prometheus-exporter` | `1.5.1` | Docker Hub 공식(nginx) | nginx 메트릭 노출 (App 서버) |
| 레포 설정 파일 | (git) | 사내 형상관리 | compose / prometheus / grafana provisioning / 문서 |

- **버전 전부 고정** (`:latest` 미사용) — `docker-compose.prod.yml` 에 핀, 재현성·변경통제 보장.
- **무결성 검증**: 번들 tar 에 대해 sha256 산출 후 반입처에서 대조 ([`install-airgap.md`](install-airgap.md) §2.3, §3). 불일치 시 설치 중단.
- **외부 플러그인 0개**: Grafana 대시보드가 코어 패널만 사용 → 추가 다운로드 자산 없음(공급망 표면 최소).

---

## 3. 네트워크 연결 명세 (방화벽 신청 근거)

| # | 출발지 | 목적지 | 포트/프로토콜 | 방향(룰 적용 위치) | 용도 | 암호화 |
|---|---|---|---|---|---|---|
| 1 | Monitoring VM | mg-was-1/2 | 9090/TCP | inbound (WAS OS 방화벽) | actuator scrape | 평문 |
| 2 | Monitoring VM | mg-was-1/2 | 9113/TCP | inbound (WAS OS 방화벽) | nginx exporter scrape | 평문 |
| 3 | 운영자 단말 | Monitoring VM | 3000/TCP | inbound (Monitoring OS 방화벽) | Grafana UI | 평문 ⚠ (§10) |
| 4 | (localhost) | WAS | 8080/TCP | 127.0.0.1 바인딩 → **룰 불필요** | nginx stub_status | — |
| 5 | (localhost) | Monitoring VM | 9090/TCP | 127.0.0.1 바인딩 → **룰 불필요** | Prometheus UI(로컬/SSH터널) | — |

**통제 요지**
- #1, #2: 각 WAS 의 메트릭 포트는 **출발지를 Monitoring VM IP 로 한정**한다. 그 외 출발지 deny.
  근거: [`install-airgap.md`](install-airgap.md) §5.3 (firewalld rich-rule, source 한정).
- #4, #5: stub_status 와 Prometheus UI 는 **127.0.0.1 바인딩**이라 외부에서 도달 불가 → 방화벽 룰 자체가 불필요.
  근거: `docker-compose.prod.yml` (prometheus `127.0.0.1:9090:9090`), [`install-airgap.md`](install-airgap.md) §5.1 (stub_status `listen 127.0.0.1:8080`).
- 같은 내부망이므로 네트워크 장비 룰 신청 없이 **호스트 OS 방화벽**만으로 통제 완결.

---

## 4. 접근통제 현황 (통제항목 → 근거 → 상태)

| 통제항목 | 근거 (설정값/위치) | 상태 |
|---|---|---|
| WAS actuator 엔드포인트 최소노출 | `exposure.include = health,info,prometheus` 화이트리스트 (setup.md §3, §7) | 적용 |
| 민감 actuator 차단 | `env`/`heapdump`/`loggers`/`beans` 매핑 안 됨, `info.env/os=false`, `health.show-details=never` (setup.md §7) | 적용 |
| actuator 포트 망 통제 | 9090 출발지 Monitoring VM 한정 (§3 #1) | 적용 |
| nginx 메트릭 포트 망 통제 | 9113 출발지 Monitoring VM 한정 (§3 #2) — **신규 통제** | 적용 |
| Prometheus UI 비노출 | 127.0.0.1 바인딩 (`docker-compose.prod.yml`) | 적용 |
| Grafana 익명 접근 차단 | `GF_AUTH_ANONYMOUS_ENABLED=false`, `GF_AUTH_DISABLE_LOGIN_FORM=false`, `GF_USERS_ALLOW_SIGN_UP=false` (`docker-compose.prod.yml`) | 적용 |
| Grafana 관리자 자격증명 분리 | `GF_SECURITY_ADMIN_PASSWORD` 를 `.env` 로 주입, `.env` chmod 600 + gitignore (install-airgap §4.2, `.gitignore`) | 적용 |
| 외부 아웃바운드 차단(Grafana) | 플러그인 admin/업데이트체크/뉴스/애널리틱스 비활성 (`docker-compose.prod.yml`) | 적용 |
| Grafana UI 전송구간 암호화 | TLS 미적용, 3000 평문 노출 | **미적용 — 잔여위험 §10** |
| 취약점(CVE) 스캔 | 수행 이력 없음 | **미수행 — 잔여위험 §10** |
| 접근/감사 로그 | 별도 수집/보존 정책 없음 | **미정의 — 잔여위험 §10** |

---

## 5. 외부 연결 차단 (격리성)

폐쇄망 격리를 깨는 아웃바운드가 없음을 보증한다.

- Prometheus: scrape 외 외부 연결 없음. 알람(alertmanager)·원격쓰기 미구성.
- Grafana: `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false`,
  `GF_NEWS_NEWS_FEED_ENABLED=false`, `GF_PLUGINS_PLUGIN_ADMIN_ENABLED=false` (`docker-compose.prod.yml`)
  → 기동 시 인터넷으로 향하는 텔레메트리/업데이트/플러그인 조회 시도 제거.
- 모든 데이터소스는 내부 Prometheus 1개(`http://prometheus:9090`, compose 내부망)뿐.

---

## 6. 데이터 분류 / 민감정보 (개인정보 미포함 근거)

수집 데이터는 **수치형 운영 메트릭**으로, 개인정보·메시지 본문을 포함하지 않는다.

| 데이터 | 민감정보 포함 여부 | 근거 |
|---|---|---|
| WAS actuator 메트릭 | 미포함 | 메트릭 라벨에 `tid`/`loginId`/`phoneNumber`/`messageBody` 추가 **금지** 규정 (setup.md §7). 라벨은 `carrier`/`result`/`outcome` 등 분류값만 |
| nginx stub_status 메트릭 | 미포함 | 노출 항목이 **집계 카운터만**(연결수/요청수). 요청 경로·클라이언트 IP·URL·헤더 일절 없음 |
| Grafana 저장 데이터 | 미포함 | 대시보드 정의·사용자 계정만. 원천 데이터 비저장(Prometheus 조회) |

→ 개인정보 영향평가 대상 데이터 없음. PII 미유입은 setup.md §7 코드리뷰 통제로 지속 보증.

---

## 7. 전송구간 암호화 현황

| 구간 | 암호화 | 근거 / 보완통제 |
|---|---|---|
| 운영자 → 메인 API (7700) | TLS 1.2 | 앱 기존 통제 (setup.md §3) — 본 스택 범위 밖 |
| Monitoring VM → WAS 9090/9113 | **평문** | 내부망 + 출발지 IP 한정 ACL + **PII 미포함**(§6)으로 위험 완화. 평문 근거는 setup.md §3 의 9090 논거를 9113 에 동일 적용 |
| 운영자 → Grafana 3000 | **평문** | **잔여위험** — §10 참조. admin 로그인 자격증명이 내부망 평문 전송됨 |
| 컨테이너 간(compose 내부) | 평문 | 단일 호스트 내부 브리지 네트워크, 외부 미노출 |

---

## 8. 무결성 / 변경통제

- 이미지·설정 **버전 고정** + sha256 무결성 검증 (§2).
- 실 IP/비밀번호 파일(`prometheus.prod.yml`, `.env`)은 **형상관리 제외**(`.gitignore`) — 비밀정보 커밋 방지. 템플릿(`*.example.yml`)만 관리.
- 설정 변경은 형상관리된 템플릿 기준 + Prometheus 무중단 reload(`/-/reload`)로 추적 (install-airgap §7).

---

## 9. 운영 연속성 / 백업

- 메트릭(TSDB)·Grafana 설정은 named volume 영속화. `down` 시 `-v` 금지 명시 (install-airgap §7).
- 볼륨 tar 백업 절차 제공 (install-airgap §7). 보관기간 `retention.time=15d` (조정 가능).

---

## 10. 잔여위험 및 수용 요청

본 구성이 **현재 미적용**으로 두고, 심의에서 수용 여부 판단을 요청하는 항목.

| # | 잔여위험 | 현재 완화요소 | 검토 옵션 (구현 시 별도 과제) |
|---|---|---|---|
| R1 | **Grafana 3000 평문 노출** — admin 자격증명·대시보드가 내부망 평문 전송 | 내부망 한정 노출, 사내 신뢰경계 | 앞단 nginx + TLS 리버스 프록시, 또는 3000 도 출발지(운영자 대역) 한정 ACL |
| R2 | **이미지 취약점(CVE) 스캔 미수행** — base OS 레이어·전이 패키지의 알려진 취약점 미확인 | 인터넷 완전 비노출(공격면 제한), 버전 고정 | 반입 전 준비 호스트에서 trivy 등 스캔 후 결과 첨부 |
| R3 | **접근/감사 로그 정책 부재** — Grafana 로그인·조회 감사추적 미정의 | 단일 admin, 내부 운영자 한정 | Grafana audit 로그 활성 + 사내 로그수집 연계 |
| R4 | scrape 구간(9090/9113) 평문 | 출발지 IP 한정 ACL + PII 미포함(§6) | 통상 내부 메트릭 수집 관행상 수용 — mTLS 는 운영부담 대비 효익 낮음 |

> R1~R3 은 사용자 요청 범위("점검·문서화")상 본 작업에서 구현하지 않았다. 심의 결과에 따라
> 별도 하드닝 과제로 진행한다.

---

## 11. 심의위원 검증용 (선택)

설치 후 통제 적용을 직접 확인:

```bash
# 익명 접근 차단 — 로그인 없이 401/302
curl -s -o /dev/null -w "%{http_code}\n" http://<monitoring-vm>:3000/api/dashboards/home   # 기대: 401

# Prometheus 외부 비노출 — VM 외부에서 접속 시 거부/타임아웃
curl -s -m 3 -o /dev/null -w "%{http_code}\n" http://<monitoring-vm>:9090/   # 기대: 연결 실패(127.0.0.1 바인딩)

# WAS 메트릭 포트 출발지 통제 — Monitoring VM 외 호스트에서 차단
curl -s -m 3 http://mg-was-1.internal:9090/actuator/prometheus   # 기대(비인가 출발지): 타임아웃/거부

# 민감 actuator 차단
curl -s -o /dev/null -w "%{http_code}\n" http://mg-was-1.internal:9090/actuator/env   # 기대: 404
```

---

## 관련 문서

- [`install-airgap.md`](install-airgap.md) — 반입·설치 상세 (방화벽 §5.3, 무결성 §2.3)
- [`setup.md`](setup.md) — §7 보안 체크리스트(원천 통제), §3 격리 모델
- `docker-compose.prod.yml` — Grafana/Prometheus 보안 설정값 원본
