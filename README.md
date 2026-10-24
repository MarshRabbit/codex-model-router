# Codex Model Router

`danweinerdev/claude-model-router`를 Codex용으로 옮긴 프로젝트입니다.
원본의 역할 분담과 단계별 승격 정책을 유지하면서, Claude 전용 후크를
Codex native 하위 에이전트와 `codex exec`용 실행기로 대체했습니다. Python 3.9 이상이
필요하며 Python 외부 패키지는 필요 없습니다. macOS/Linux를 대상으로 합니다.
Codex 앱 안에서는 native 경로, 일반 터미널/CI에서는 CLI 경로를 사용합니다.

```text
요청 → 읽기 전용 라우터 → 작업별 역할·모델·추론 강도 배정
     → 코드 작업 → 승인된 검사 명령 → 별도 읽기 전용 리뷰
         통과: 다음 작업 / 완료
         구현 실패: 같은 역할을 다음 모델·추론 프로필로 재실행
         환경·요구사항 문제: needs_input으로 중단
```

## 일반 터미널에서 실행

이 폴더를 원하는 위치에 두고, 실제 코드를 수정할 프로젝트를 `--project`로 지정합니다.
현재 폴더와 대상 프로젝트가 달라도 됩니다. 경로에 공백이 있으면 인용부호로 감쌉니다.

```bash
python3 router.py doctor
python3 router.py routes --project /path/to/project

# 읽기 전용으로 계획만 생성
python3 router.py plan --project /path/to/project "실패하는 로그인 테스트의 원인을 고치고 회귀 테스트를 추가해줘"

# 자동 분해 → 모델 배정 → 구현 → 검증 → 필요 시 승격
python3 router.py run --project /path/to/project \
  --check 'python3 -m unittest discover -s tests' \
  "실패하는 로그인 테스트의 원인을 고치고 회귀 테스트를 추가해줘"

# 이미 범위가 정해진 작업: 앞단 분해 생략, builder 역할 사용
python3 router.py run --project /path/to/project --agent builder \
  --check 'npm test' "명세에 따라 입력 검증 오류를 수정해줘"
```

검사 명령은 예시이므로 실제 프로젝트 명령을 사용합니다. `--check`는 반복할 수 있고,
설정 파일의 검사에 추가됩니다. 명령은 `shlex.split`으로 argv로 분리하여 실행하며,
`&&`, 파이프, 리다이렉션 등을 쉘 구문으로 자동 해석하지 않습니다. 쉘이 꼭 필요하면
신뢰할 수 있는 프로젝트 설정의 argv에 `bash`, `-lc`, 명령문을 명시합니다.
검사 명령은 호출한 사용자의 로컬 권한으로 실행됩니다. 모델이 제안한 임의의 명령을
검사 설정으로 자동 채택하지 않습니다.

긴 요청은 `--task-file request.txt`로 전달할 수 있습니다. 검토한 계획은 다음처럼 실행합니다.

```bash
python3 router.py run --project /path/to/project \
  --plan-file /path/to/project/.codex/router-runs/RUN_ID/plan.json \
  --task-file request.txt
```

`--plan-file`은 구조와 순서·의존성을 다시 검증합니다. 검사 명령과 예산은 계획 파일이
아니라 실행 시작 시 읽은 설정에서 가져옵니다. 사용자 변경 사항이나 이전 시도의 수정은
자동 되돌리지 않으며, 커밋·푸시·배포를 자동 수행하도록 지시하지 않습니다.

## Codex 대화에서 사용하도록 설치

```bash
python3 router.py install --project /path/to/project --enable-default
```

프로젝트 안에 다음 파일을 설치합니다.

- `.agents/skills/codex-model-router/`: 스킬과 자체 실행기·역할·기본 설정
- `.codex/agents/router_*.toml`: Codex에서 직접 호출할 수 있는 전문 역할
- `.codex/model-router.json`: 프로젝트의 모델·추론 강도·검사·예산 설정
- `AGENTS.md`: `--enable-default`를 지정한 경우에만 표시된 라우팅 지침을 추가

기존 `AGENTS.md`의 내용과 `.codex/config.toml`은 보존합니다. 같은 이름의 다른 파일이
있으면 덮어쓰지 않고 중단합니다. 동일 파일을 다시 설치하는 것은 안전합니다.
이 설치기는 프로젝트 범위이며 `~/.codex`나 전역 설정을 수정하지 않습니다.
설치 후 프로젝트를 다시 열거나 새 Codex 대화를 시작해 스킬과 역할을 발견하게 합니다.

대화 예시:

```text
$codex-model-router를 사용해 이 버그를 수정해줘.
검사는 python3 -m unittest discover -s tests로 실행해줘.
```

스킬은 앱 안에서 **Codex native 하위 에이전트 + Python 상태 감독기**를 사용합니다.
macOS에서는 Codex의 셸 샌드박스 안에서 `codex exec`를 다시 시작할 때
`sandbox-exec: sandbox_apply: Operation not permitted`가 발생할 수 있으므로,
앱 안에서는 중첩 CLI 실행을 기본 경로로 사용하지 않습니다. 샌드박스를 해제해 우회하지 않습니다.

native 흐름은 부모 Codex가 초기 작업을 분해·배정하고, 감독기가 출력한 다음 action의
모델·추론 강도로 실제 작업자와 별도 리뷰어를 호출하는 방식입니다. 감독기는 검사 명령을
직접 실행하고, 단계·티켓·시도 한도·실패 증거를 검사해 완료/승격을 결정합니다.
동일 작업자 ID로 자기 리뷰를 제출하거나 소비한 티켓을 재사용할 수 없습니다.
프로토콜 전체는 [native 사용 절차](skills/codex-model-router/references/native.md)에 있습니다.

```bash
# Codex가 이 응답의 action에 따라 실제 하위 에이전트를 호출
python3 router.py native start --project /path/to/project --agent builder \
  --task-file request.txt --check 'python3 -m unittest discover -s tests'

# 대기 중인 action 조회 (추가 호출을 발급하지 않음)
python3 router.py native status --project /path/to/project --run-id RUN_ID
```

단순히 native `router_builder` 같은 역할을 직접
호출한 경우에는 부모 Codex가 검증과 재배정을 책임지며, 실행기의 강제 검증 루프를
사용한 것으로 간주하지 않습니다. native 에이전트는 부모의 권한을 상속합니다.
native 역할의 읽기 전용 지침은 권한 경계가 아니며, 실행기 모드에서는 실제
`--sandbox read-only`를 planner·scout·extractor·sage·reviewer에 적용합니다.
native 감독기는 하위 에이전트 ID/실제 모델을 인증하는 서비스가 아닙니다. 부모가 실제
도구에서 받은 결과를 그대로 제출해야 하며, 수동 작성한 보고서를 실제 실행으로 표시하면 안 됩니다.

## 모델과 추론 강도

기본 프로필은 다음과 같습니다. 작업별 최적 성능을 입증한 벤치마크가 아니라 변경 가능한
시작 설정입니다. 계정에서 사용할 수 없는 모델은 프로젝트 설정에서 교체해야 합니다.
`doctor`는 CLI 옵션과 로그인 상태만 확인하고 모든 모델 접근 권한을 입증하지 않습니다.

| 순서 | 프로필 | 모델 | 추론 강도 |
|---|---|---|---|
| 1 | light | gpt-6-luna | max |
| 2 | standard | gpt-6.1-sol | medium |
| 3 | deep | gpt-6.1-sol | high |
| 4 | ceiling | gpt-6-astra | medium |

`scout`·`extractor`는 light, `mechanic`·`builder`는 standard,
`sage`는 deep에서 시작합니다. planner는 standard, 독립 reviewer는 deep입니다.
승격 시 역할은 유지하고 다음 프로필을 사용하므로 **모델 변경과 추론 강도 변경을 모두**
지원합니다. 설정의 `tiers` 배열 순서가 승격 순서이며, 각 모델·추론 쌍은 달라야 합니다.
각 역할의 기본 tier보다 낮은 초기 배정은 거절합니다.

마지막 ceiling 프로필은 자동 초기 선택을 금지합니다. 사용자가 의도적으로 바로 쓰려면
`--agent builder --tier ceiling`처럼 명시합니다. 실패 후의 승격에는 실제 검사 실패나
독립 리뷰의 거절이 필요하며, 프롬프트의 승격 문자열만으로 허용하지 않습니다.

프로젝트에 `.codex/model-router.json`이 없으면 번들 기본값을 사용합니다. 설치 시에는
편집하기 쉽도록 전체 설정을 생성합니다. 직접 만드는 파일은 필요한 키만 덮어써도 됩니다.

```json
{
  "checks": [
    {"name": "tests", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout_seconds": 120}
  ],
  "policy": {"max_attempts": 3, "max_codex_calls": 16, "max_run_seconds": 1800}
}
```

`agents`는 역할별로 병합되고 `checks`와 `tiers` 배열은 통째로 교체됩니다. 사용자 정의
역할은 `tier`, `sandbox`, `capabilities`, `instructions`를 등록합니다. 예를 들어:

```json
{
  "agents": {
    "sql_reviewer": {
      "tier": "deep",
      "sandbox": "read-only",
      "capabilities": ["review-sql-migration"],
      "instructions": "Review migration correctness, locking, and rollback risks. Do not modify files. Cite concrete evidence."
    }
  }
}
```

모델 이름과 각 모델이 지원하는 추론 강도는 계정·버전에 따라 달라질 수 있습니다.
프로필을 바꾼 뒤 작은 작업으로 확인합니다. 지원되지 않는 모델·권한·네트워크·출력 오류는
무조건 상위 모델에 재시도하지 않고 `execution_failed`로 중단합니다.

## 검증과 중단 조건

작업마다 다음 조건을 모두 충족해야 `completed`가 됩니다.

1. CLI는 성공 종료·`turn.completed`·올바른 JSON, native는 발급한 action에 대한 실제 작업자의 구조화 응답을 제출
2. worker가 작업 완료를 보고함
3. 실행 시작 시 스냅샷한 검사 명령이 모두 성공 종료함
4. 별도 읽기 전용 Codex 호출이 실제 파일·출력과 검사 결과를 검토해 승인함

검사가 없으면 독립 리뷰만 수행하며 결과에 `independent-review-only`라고 기록합니다.
모델 리뷰는 정확성을 보장하지 않으므로 가능한 경우 인수 테스트를 지정합니다.
실패한 검사 결과를 reviewer의 승인이나 worker의 자기 보고로 덮어쓸 수 없습니다.
CLI의 검사·정책 설정은 메모리에 스냅샷하므로 worker가 설정 파일을 수정해도 바뀌지 않습니다.
native는 시작 시 저장한 설정 스냅샷을 사용하며, 작업자가 감독기 파일을 편집하지 않도록 역할을 제한합니다.
이는 테스트 자체나 작업 공간을 악성 코드로부터 보호하는 보안 장치는 아닙니다.

기본 한도는 작업 8개, 작업당 시도 3회, Codex 호출 24회, 전체 30분,
개별 Codex 호출 10분, 개별 검사 2분입니다. 각 재시도는 실제로 다른 프로필을 사용합니다.
재시도에는 이전 출력과 검사 실패·독립 리뷰의 피드백을 넘깁니다. 환경 또는 요구사항
문제는 `needs_input`으로 멈춥니다. 마지막 시도 실패 시 작업을 완료 처리하지 않습니다.
현재 작업 파일에 대한 동시 실행은 잠금/활성 native 실행 기록으로 차단합니다.
native 호출 자체의 중단·시간 초과는 부모 Codex가 실제 하위 에이전트를 중단해야 합니다.
감독기는 결과 제출/상태 조회 시 전체 시간 한도를 검사하고 다음 action 발급을 거절할 수 있지만,
Codex 도구에 접근하여 실행 중인 native 하위 에이전트를 강제로 종료하지는 않습니다.

| 종료 코드 | 의미 |
|---|---|
| 0 | 모든 작업 검증 완료 또는 `plan` 생성 완료 |
| 1 | 설정·CLI·응답·검사 실행 환경 오류 |
| 2 | 검증 실패, 재시도/프로필 소진 |
| 3 | 사용자 판단 또는 환경 조치 필요 |
| 4 | 호출/시간 예산 소진 |
| 130 | 사용자가 실행 중단 |

실패 후에는 `.codex/router-runs/RUN_ID/summary.json`을 먼저 확인합니다. 한도를 초기화하려고
같은 실행을 자동 반복하지 않습니다. 강제 종료 후 잠금이 남으면 파일의 PID가 실제로
종료했는지 확인한 뒤 해당 잠금만 제거합니다. 중단 후 자동 resume는 구현하지 않았습니다.

## 기록과 비용

각 실행은 대상 프로젝트의 `.codex/router-runs/RUN_ID/`에 계획, 설정 스냅샷,
worker/reviewer 응답, 검사 stdout/stderr, 이벤트, 최종 상태를 저장합니다.
프롬프트와 코드 경로가 로컬 기록에 포함되므로 저장소에 커밋하지 않도록 이 경로와
`.codex/model-router.lock` 및 `.codex/model-router-active.json`을 프로젝트 `.gitignore`에 추가하는 편이 좋습니다.
모델에 전달하는 정보와 인증은 Codex CLI가 처리합니다.

```bash
python3 router.py stats --project /path/to/project
```

CLI 통계는 Codex가 보고한 토큰과 호출 수·승격 횟수입니다. native는 발급한 action과
부모가 제출한 에이전트 ID를 기록하며 토큰을 추정하지 않습니다. 실제 달러 비용이나 절감률을
추정하지 않습니다. planner·독립 reviewer 호출도 비용과 시간이 들며, 아주 작은 작업에는
단일 모델보다 비효율적일 수 있습니다. 병렬 편집이나 자동 배포 기능은 추가하지 않았습니다.

## 테스트

```bash
make check
```

외부 의존성 없는 unittest와 가짜 Codex JSONL 프로토콜로 실제 파일 수정 → 실제
검사 명령 실패 → 다른 프로필 배정 → 성공까지 검증합니다. 실패 한도, 허위 성공 보고,
설정 변경, CLI 오류, 예산, 프로세스 시간 초과와 자식 종료, 프로젝트 설치도 검사합니다.
오프라인 테스트는 실제 모델 성능이나 계정 접근 권한을 증명하지 않습니다.

## 이식 근거

- 원본: [danweinerdev/claude-model-router](https://github.com/danweinerdev/claude-model-router),
  v0.3.0, `7091881a48136cf06b3a483399dd370b1f7d9360`
- [Codex 비대화형 실행·구조화 출력](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex 사용자 정의 에이전트·모델·추론 설정](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI의 Codex 반복 복구 루프 예제](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)

원본의 MIT LICENSE와 NOTICE를 보존했습니다. Claude 전용 후크·에이전트 frontmatter·
상태 표시줄 설정·고정 가격 계산은 사용하지 않습니다. 세부 출처는 NOTICE를 참고합니다.
