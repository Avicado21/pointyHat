#!/bin/bash
# logs.sh — tail all PointyHat Lambda logs in parallel
# Usage:
#   ./logs.sh          # last 5 minutes
#   ./logs.sh 15m      # last 15 minutes
#   ./logs.sh 1h       # last 1 hour

SINCE=${1:-5m}
PROFILE=your-aws-profile

LAMBDAS=(
  pointyhat-createJob
  pointyhat-writer
  pointyhat-vision
  pointyhat-ambiguity
  pointyhat-hacksearch
  pointyhat-ranker
  pointyhat-resumeJob
  pointyhat-checkJob
)

echo "Tailing all PointyHat logs (since $SINCE)..."
echo "Press Ctrl+C to stop"
echo "────────────────────────────────────────"

for fn in "${LAMBDAS[@]}"; do
  aws logs tail "/aws/lambda/$fn" \
    --profile $PROFILE \
    --since $SINCE \
    --format short \
    2>/dev/null | sed "s/^/[$fn] /" &
done

wait
