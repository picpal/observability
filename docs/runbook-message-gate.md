# message-gate 관측 운영 런북 (Phase 1)

**사용 시점**: 알람을 받았거나 대시보드에 이상이 보일 때 첫 30초 안에 펼친다.
상세 설계/협업 절차는 `setup.md` 를 본다. 본 문서는 일상 대응 1페이지.

## 운영 구성 요약

| 구성 | 값 | 위치 |
|---|---|---|
| message-gate 인스턴스 | Active-Active 2대 (운영 표준) | `mg-was-1.internal`, `mg-was-2.internal` |
| 앱 포트 | 7700 (TLS 1.2, 메인 API) | 사내 ACL 으로 외부 차단 |
| 관측 포트 | 9090 (평문, Prometheus scrape 전용) | 사내 Prometheus IP 만 allow |
| Prometheus | 1대, scrape interval 10s | 사내 운영 |
| Grafana 폴더 | `Dashboards/message-gate` | 사내 운영 |

**중요한 라벨 구분** — 알람을 보거나 PromQL 을 쓸 때 자주 혼동된다.

| 라벨 | 의미 | 예시 |
|---|---|---|
| `instance` | message-gate 가 떠 있는 WAS 호스트 | `mg-was-1.internal:9090` |
| `carrier` | 메시지가 어느 통신사로 dispatch 됐는지 (앱 내부 분기) | `KT`, `LG`, `OLD_LG` |

**WAS 2대는 둘 다 동일한 message-gate 빌드**이며 같은 carrier 들을 호출한다. KT 전용 WAS / LG 전용 WAS 같은 분리는 없다. 모든 패널이 `{{instance}} / {{carrier}}` legend 를 쓰는 이유 = 두 축의 곱이라서.

## 대시보드 보는 순서 (정상/이상 판정)

| 순서 | 패널 | 정상 | 즉시 조치 트리거 |
|---|---|---|---|
| 1 | `instance up` | 두 인스턴스 모두 `UP` | 하나라도 DOWN → Step A |
| 2 | 패널 3 CircuitBreaker | 모든 (instance, carrier) state = closed | `open` 출현 → Step B |
| 3 | 패널 1 성공률 | per-instance × carrier 모두 ≥ 95% | 한 라인만 떨어지면 인스턴스 또는 통신사 한쪽 문제 |
| 4 | 패널 2 p95/p99 | KT/LG p95 < 500ms | p95 ≥ 1s → Step C |
| 5 | 패널 4 hit ratio | 각 instance ≥ 90% | 한쪽만 낮으면 그 인스턴스 캐시 워밍 부족 |
| 6 | 패널 6 idempotency | 0 근처 | 0 이상이 지속 → 클라이언트 재시도 패턴 또는 TID 충돌 |

패널 5 (Grace NEW/OLD) 는 평시 0. OLD 비율이 1주일 내 0 으로 떨어지지 않으면 운영팀에 비밀번호 회전 안내.

## 즉시 조치 — Step A/B/C

### Step A — 한 인스턴스 DOWN

1. `curl -m 3 http://mg-was-N.internal:9090/actuator/health` 직접 확인 (방화벽 회피 가능성).
2. 다른 인스턴스 (UP 쪽) 의 `mg_carrier_send_seconds_count` 값이 두 배로 뛰는지 확인 — LB 가 정상적으로 다른 쪽으로 모는 중인지.
3. DOWN 인스턴스 호스트 OS 측 점검: `systemctl status message-gate`, `journalctl -u message-gate -n 100`.
4. 재기동 후에도 안 올라오면 escalation (아래).

### Step B — CircuitBreaker OPEN

1. 어느 `(instance, name)` 이 OPEN 인지 패널 3 에서 식별.
2. 패널 2 가 같은 carrier 의 p95 가 함께 뛰었으면 → 통신사 측 장애. 사내 통신사 담당자에게 알림.
3. p95 정상이고 OPEN 만 떴으면 → minimum-number-of-calls 미달 상태에서 운영 변화 가능성. `resilience4j_circuitbreaker_failure_rate` 값을 보고 슬라이딩 윈도우 안의 실패율 확인.
4. Manual failover 가 필요하면 `POST /api/v1/admin/failover` (운영 가이드 별도 챕터).

### Step C — p95 지연 폭증

1. `histogram_quantile(0.99, sum by (instance, carrier, le) (rate(mg_carrier_send_seconds_bucket[5m])))` 으로 p99 도 같이 뛰는지.
2. 한 instance 만 튀면 → 그 호스트의 GC/스레드/네트워크 점검.
3. 양 instance 모두 같은 carrier 에서 튀면 → 통신사 측 응답 지연. CarrierAdapter timeout 조정은 코드 변경 사항.

## 자주 쓰는 PromQL (Prometheus 콘솔에서 즉시 사용)

```promql
# 인스턴스 살아있는지
up{job="message-gate"}

# 최근 5분간 carrier × instance 별 호출 분포
sum by (instance, carrier) (rate(mg_carrier_send_seconds_count[5m]))

# 인스턴스별 인증 캐시 상태
sum by (instance, outcome) (rate(mg_auth_cache_total[5m]))

# CB open 인 (instance, carrier) 만 추출
resilience4j_circuitbreaker_state{state="open"} == 1

# 중복 TID 거부가 발생 중인지
sum by (instance) (rate(mg_idempotency_duplicate_total[5m])) > 0
```

## 운영 변경 시 — 인스턴스 추가/제거

**메시지 게이트 인스턴스를 N → N±1 로 바꾸려면 prometheus.yml 의 `targets[]` 만 수정**한다.

```yaml
scrape_configs:
  - job_name: message-gate
    static_configs:
      - targets:
          - mg-was-1.internal:9090
          - mg-was-2.internal:9090
          - mg-was-3.internal:9090   # 추가 시 한 줄
```

리로드는 `curl -X POST http://prom-server.internal:9090/-/reload` 또는 `kill -HUP <prometheus-pid>`. 재기동 불필요. Grafana 측은 자동으로 새 instance 라벨이 모든 패널의 분리 라인에 추가된다.

## Escalation

| 상황 | 1차 | 2차 |
|---|---|---|
| WAS OS/네트워크 | 인프라팀 | SRE |
| 통신사 측 장애 (KT/LG) | 통신사 담당자 | 사업팀 |
| message-gate 앱 버그 의심 | 개발팀 (PR/Issue) | — |
| Prometheus/Grafana 자체 다운 | 인프라팀 (관측 인프라 담당) | — |

## 참고

- 패널/PromQL 전체: `setup.md` §5
- 알람 룰: `setup.md` §6
- 인프라 협업 체크리스트: `setup.md` §2
- 신규 서비스(다른 도메인) 붙이기: `setup.md` §5-A.1
