# PointyHat

AI-powered hat search built on AWS. Upload an outfit photo or describe what you're looking for — PointyHat finds the top 3 matching hats across the web.

> Developed in June 2026 as a proof-of-concept personal project, powered by the AWS CIC. Made public October 2026.

---

## What it does

**Text search** — type a query like `"black fitted pirates hat"` and get ranked results with reasoning.

**FitFinder (image search)** — upload a photo of your outfit. A vision agent analyzes the colors and style, suggests hat types that would complement the look, and searches once you pick a style.

Both paths run through an ambiguity/preference check before searching, so results are always specific rather than broad.

---

## Architecture

```
CloudFront → S3 (index.html)
CloudFront → API Gateway
  POST /search      → createJob      → Step Functions execution
  POST /resume      → resumeJob      → SendTaskSuccess (resumes SF)
  GET  /job/{jobId} → checkJob       → DynamoDB read

Step Functions Standard Workflow (pointyhat-pipeline):
  writerLambda (PENDING)
  → JobRouter (Choice state)
      image → visionLambda → writerLambda (CLARIFICATION, waitForTaskToken)
      text  → ambiguityLambda
                → ambiguous → writerLambda (CLARIFICATION, waitForTaskToken)
                → clear     → hackSearchLambda
  → [user answers via POST /resume]
  → hackSearchLambda (Brave Search + Shopify + URL filter)
  → rankerLambda (Sonnet picks top 3)
  → writerLambda (COMPLETE)
  any error → writerLambda (FAILED)
```

### Lambda inventory

| Lambda | Purpose |
|---|---|
| `createJob` | Validates input, starts SF execution |
| `visionLambda` | Analyzes outfit image (Sonnet vision) |
| `ambiguityLambda` | Checks if text query needs clarification |
| `hackSearchLambda` | Brave Search + Shopify scraping |
| `rankerLambda` | Picks top 3 recommendations (Sonnet) |
| `writerLambda` | All DynamoDB writes |
| `resumeJob` | Resumes SF execution after clarification |
| `checkJob` | Polls job status for frontend |

### AWS services used

- **Step Functions** (Standard Workflow) — orchestration, human-in-the-loop via `waitForTaskToken`
- **Lambda** (Python 3.12, ARM64) — all compute
- **Bedrock** (Claude Sonnet 4.5, cross-region inference) — vision, ambiguity, ranking
- **DynamoDB** — job state store (24hr TTL)
- **API Gateway** — REST API
- **S3 + CloudFront** — frontend hosting
- **SSM Parameter Store** — secrets (Brave API key, Logfire token)
- **Logfire** — Pydantic AI tracing (all agent runs)

---

## Repo structure

```
pointyhat/
├── bin/
│   └── pointyhat.ts          # CDK app entry
├── lib/
│   └── pointyhat-stack.ts    # Full CDK stack
├── lambda/
│   ├── createJob/
│   │   ├── createJob.py
│   │   └── requirements.txt
│   ├── visionLambda/
│   │   ├── visionLambda.py
│   │   └── requirements.txt
│   ├── ambiguityLambda/
│   │   ├── ambiguityLambda.py
│   │   └── requirements.txt
│   ├── hackSearchLambda/
│   │   ├── hacksearch.py
│   │   └── requirements.txt
│   ├── rankerLambda/
│   │   ├── rankerLambda.py
│   │   └── requirements.txt
│   ├── writerLambda/
│   │   ├── writerLambda.py
│   │   └── requirements.txt
│   ├── resumeJob/
│   │   ├── resumeJob.py
│   │   └── requirements.txt
│   └── checkJob/
│       ├── checkJob.py
│       └── requirements.txt
├── site/
│   └── index.html            # Frontend (API URL injected at deploy)
├── benchmarks/               # Search pipeline benchmarks
│   ├── BraveBenchmark.py
│   ├── DuckCrawlTest.py
│   └── BraveCrawlTest.py
├── logs.sh                   # Tail all Lambda logs in parallel
├── README.md
├── .gitignore
└── package.json
```

---

## Prerequisites

- AWS account + CLI configured (`aws sso login`)
- Node.js 18+ and CDK (`npm install -g aws-cdk`)
- Docker Desktop (for Lambda bundling)
- Python 3.12+

### SSM parameters (create before deploying)

```bash
aws ssm put-parameter \
  --name /pointyhat/brave-api-key \
  --value <your-brave-api-key> \
  --type SecureString \
  --profile <your-profile>

aws ssm put-parameter \
  --name /pointyhat/logfire-token \
  --value <your-logfire-token> \
  --type SecureString \
  --profile <your-profile>
```

Get a Brave Search API key at [brave.com/search/api](https://brave.com/search/api).
Get a Logfire token at [logfire.pydantic.dev](https://logfire.pydantic.dev).

---

## Deploy

```bash
npm install
cdk bootstrap --profile <your-profile>   # first time only
cdk deploy --profile <your-profile>
```

CDK injects the API Gateway URL into `site/index.html` at deploy time, uploads to S3, and invalidates CloudFront.

Outputs:
- `ApiUrl` — API Gateway base URL
- `WebsiteUrl` — CloudFront distribution URL
- `StateMachineArn` — Step Functions ARN (view executions in AWS console)

---

## Local testing

```bash
# Tail all Lambda logs in parallel
./logs.sh        # last 5 minutes
./logs.sh 15m    # last 15 minutes
./logs.sh 1h     # last hour

# Watch Step Functions executions
aws stepfunctions list-executions \
  --state-machine-arn <StateMachineArn> \
  --profile <your-profile> \
  --max-results 5
```

---

## Lambda requirements

| Lambda | Dependencies |
|---|---|
| `createJob` | stdlib only |
| `visionLambda` | `pydantic>=2.0`, `pydantic-ai-slim[bedrock]`, `logfire[pydantic-ai]` |
| `ambiguityLambda` | `pydantic>=2.0`, `pydantic-ai-slim[bedrock]`, `logfire[pydantic-ai]` |
| `hackSearchLambda` | `pydantic>=2.0` |
| `rankerLambda` | `pydantic>=2.0`, `pydantic-ai-slim[bedrock]`, `logfire[pydantic-ai]` |
| `writerLambda` | stdlib only |
| `resumeJob` | stdlib only |
| `checkJob` | stdlib only |

---

## Search pipeline benchmarks

Three benchmark scripts in `benchmarks/` compare different search + scraping approaches across 5 test queries:

| System | Avg time | Prices found | Relevance (high) |
|---|---|---|---|
| Brave + urllib | ~20s | 12/75 | 14/15 |
| DDG + Crawl4AI | ~34s | 32/75 | 13/15 |
| Brave + Crawl4AI | ~37s | 42/75 | 11/15 |

Brave + urllib is the current production approach — fastest, most reliable, best relevance.

---

## Notes

- Images are base64 encoded in the browser before upload. Large images (>150KB base64) are stored in DynamoDB; smaller ones are inlined in the Step Functions execution payload.
- DynamoDB TTL is 24 hours — job records and images are automatically cleaned up.
- The Step Functions `waitForTaskToken` pattern pauses the execution at the clarification state until the user answers. The task token is stored in DynamoDB and consumed by `resumeJob` via `SendTaskSuccess`.
- All Pydantic AI agent runs (vision, ambiguity, ranking) are traced in Logfire automatically via `instrument_pydantic_ai()`.

---

## Built with

- [AWS CDK](https://aws.amazon.com/cdk/) — infrastructure as code
- [Pydantic AI](https://ai.pydantic.dev/) — structured LLM outputs + Logfire tracing
- [Anthropic Claude Sonnet 4.5](https://www.anthropic.com/) via Amazon Bedrock
- [Brave Search API](https://brave.com/search/api/) — web search
- [Logfire](https://logfire.pydantic.dev/) — observability
