#!/usr/bin/env bash
# check-downloads.sh - Print whether the Ollama and MLX background downloads
# are still running, and whether the model files are fully on disk.
#
# Exit code 0  = both done
# Exit code 1  = at least one still in progress
# Exit code 2  = at least one failed (proc gone but files missing)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; DIM=$'\033[2m'; RESET=$'\033[0m'

# ---------- Ollama ----------------------------------------------------------
ollama_done=0
ollama_running=0

# A finished model has its tag listed by `ollama list`. We accept any of the
# candidates the installer might have fallen through to. This check is
# independent of process state: a successful HF -> ollama-create import leaves
# no running processes but DOES leave a registered tag, so we must trust
# `ollama list` as the source of truth.
if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qE "^(qwen3-coder-next|qwen3-coder):"; then
  ollama_done=1
  ollama_tag_found=$(ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -E "^(qwen3-coder-next|qwen3-coder):" | head -1)
fi

ollama_pids=$(pgrep -f "ollama pull" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)
ollama_resume_pids=$(pgrep -f "scripts/resume-ollama.sh" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)
ollama_hf_pids=$(pgrep -f "scripts/download-ollama-from-hf.sh" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)

if [[ -n "$ollama_pids" ]]; then
  ollama_running=1
  ollama_pull_tags=$(ps -o command= -p "$ollama_pids" 2>/dev/null \
    | awk '{ for (i=1;i<=NF;i++) if ($i ~ /:/) print $i }' \
    | sort -u | paste -sd, -)
  if [[ -z "$ollama_pull_tags" ]]; then
    ollama_pull_tags="(in progress)"
  fi
elif [[ -n "$ollama_resume_pids" ]]; then
  ollama_running=1
  ollama_pull_tags=$(ps -o command= -p "$ollama_resume_pids" 2>/dev/null \
    | awk '{ for (i=1;i<=NF;i++) if ($i ~ /:/) print $i }' \
    | sort -u | paste -sd, -)
  if [[ -z "$ollama_pull_tags" ]]; then
    ollama_pull_tags="(resume loop, sleeping)"
  else
    ollama_pull_tags="$ollama_pull_tags (resume loop, sleeping)"
  fi
elif [[ -n "$ollama_hf_pids" ]]; then
  # HF-driven path: the wrapper script is doing snapshot_download via hf_transfer.
  ollama_running=1
  ollama_pull_tags="(via hf_transfer, see .logs/install-ollama-from-hf.log)"
fi

# Pull the most recent self-reported progress line out of THE most recently
# modified install log (`ollama pull` prints a TTY progress bar with embedded
# %, B/s, ETA). Reading older logs gives stale data from prior runs.
#
# Ollama writes the progress bar via \r-terminated lines with embedded
# CSI sequences like \x1b[K and \x1b[?2026l. The strip needs to handle
# all CSI codes (any final char in [a-zA-Z]), not just SGR (m).
ollama_self_progress=""
shopt -s nullglob
ollama_logs=("$REPO_ROOT/.logs/install-ollama"*.log)
shopt -u nullglob
if (( ${#ollama_logs[@]} > 0 )); then
  newest_log=$(ls -t "${ollama_logs[@]}" 2>/dev/null | head -1)
  if [[ -n "$newest_log" ]]; then
    ollama_self_progress=$(
      sed 's/\r/\n/g; s/\x1b\[[0-9;?]*[a-zA-Z]//g' "$newest_log" 2>/dev/null \
        | grep -oE "pulling [a-f0-9]+: *[0-9]+%[^[:cntrl:]]*" \
        | tail -1 \
        | sed -E 's/[[:space:]]+pulling manifest.*$//; s/[[:space:]]+$//' \
        || true
    )
  fi
fi

# Per-blob breakdown.
#
# IMPORTANT: Ollama uses sparse files for partial blobs (it truncates the file
# to the full target size up front, then fills holes as chunks arrive). That
# means the *apparent* file size from `ls -lh` or `stat -f %z` is the FINAL
# size, not the downloaded size. The truthful number is the allocated-block
# count (`stat -f %b` * 512).
#
# We show: <short-hash>  <allocated GB> / <apparent GB>  (<percent>%)
ollama_blob_lines=""
if [[ -d "$HOME/.ollama/models/blobs" ]]; then
  ollama_blob_lines=$(
    find "$HOME/.ollama/models/blobs" -maxdepth 1 -name "sha256-*-partial" \
         ! -name "*-partial-[0-9]*" 2>/dev/null \
      | while read -r f; do
          stat -f "%N %z %b" "$f"
        done \
      | awk '{
          n=split($1,a,"/"); fname=a[n];
          sub(/^sha256-/,"",fname); sub(/-partial$/,"",fname);
          short=substr(fname,1,12);
          apparent=$2/1024/1024/1024;
          allocated=($3*512)/1024/1024/1024;
          pct = (apparent > 0) ? (allocated/apparent*100) : 0;
          printf "%s  %5.1f GB / %5.1f GB  (%5.1f%%)\n", short, allocated, apparent, pct;
        }' \
      | sort -k4 -rn
  )
fi

# Sum allocated bytes across partial blobs to get the real "downloaded so far"
# total (du also undercounts sparse files but in the opposite direction).
ollama_alloc_gb="0"
if [[ -d "$HOME/.ollama/models/blobs" ]]; then
  ollama_alloc_gb=$(
    find "$HOME/.ollama/models/blobs" -maxdepth 1 -name "sha256-*-partial" \
         ! -name "*-partial-[0-9]*" 2>/dev/null \
      | while read -r f; do stat -f "%b" "$f"; done \
      | awk '{ s += $1*512 } END { printf "%.1f", s/1024/1024/1024 }'
  )
fi
ollama_disk=$(du -sh "$HOME/.ollama/models" 2>/dev/null | awk '{print $1}')

# When the active downloader is the HF path, the registry blob count is 0 and
# the real progress lives under models/<repo-slug>/. Compute that and use it
# as the headline number so `make downloads-watch` actually shows movement.
hf_target=""
hf_target_gb="0"
hf_target_bytes=0
if [[ -n "${ollama_hf_pids:-}" ]] && [[ -f "$REPO_ROOT/.logs/install-ollama-from-hf.log" ]]; then
  # The log uses ANSI color codes around the [hf-ollama] tag; strip them before
  # matching so the regex works on real terminal output.
  hf_target=$(sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$REPO_ROOT/.logs/install-ollama-from-hf.log" 2>/dev/null \
    | grep -E "^\[hf-ollama\] target:" \
    | tail -1 \
    | awk '{print $NF}')
  if [[ -n "$hf_target" && -d "$hf_target" ]]; then
    # Sum every regular file in the target tree, including
    # .cache/huggingface/download/<hash>.incomplete which is where hf_transfer
    # writes the in-flight payload (it's only renamed to <name>.gguf at the end).
    hf_target_bytes=$(find "$hf_target" -type f -print0 2>/dev/null | xargs -0 stat -f "%z" 2>/dev/null | awk '{ s += $1 } END { print s+0 }')
    hf_target_gb=$(awk -v b="$hf_target_bytes" 'BEGIN { printf "%.2f", b/1024/1024/1024 }')
    # Headline: prefer the HF byte count when the registry path is empty.
    if [[ "$ollama_alloc_gb" == "0" || "$ollama_alloc_gb" == "0.0" ]]; then
      ollama_alloc_gb="$hf_target_gb"
    fi
  fi
fi

# Delta vs previous poll, so the watch loop shows live throughput even while
# hf_transfer batches its disk flushes.
state_dir="$REPO_ROOT/.logs"
state_file="$state_dir/check-downloads.state"
mkdir -p "$state_dir"
ollama_throughput=""
ollama_combined_bytes=0
if [[ -d "$HOME/.ollama/models/blobs" ]]; then
  reg_bytes=$(
    find "$HOME/.ollama/models/blobs" -maxdepth 1 -name "sha256-*-partial" \
         ! -name "*-partial-[0-9]*" 2>/dev/null \
      | while read -r f; do stat -f "%b" "$f"; done \
      | awk '{ s += $1*512 } END { print s+0 }'
  )
  ollama_combined_bytes=$reg_bytes
fi
if (( hf_target_bytes > 0 )); then
  ollama_combined_bytes=$(( ollama_combined_bytes + hf_target_bytes ))
fi
now_epoch=$(date +%s)
if [[ -f "$state_file" ]]; then
  prev_line=$(tail -1 "$state_file" 2>/dev/null)
  prev_ts=$(awk '{print $1}' <<<"$prev_line")
  prev_bytes=$(awk '{print $2}' <<<"$prev_line")
  if [[ -n "$prev_ts" && -n "$prev_bytes" ]] && (( now_epoch > prev_ts )); then
    delta=$(( ollama_combined_bytes - prev_bytes ))
    interval=$(( now_epoch - prev_ts ))
    if (( delta > 0 )) && (( interval > 0 )) && (( interval < 600 )); then
      ollama_throughput=$(awk -v d="$delta" -v i="$interval" \
        'BEGIN {
           mbps = d/1024/1024/i;
           if (mbps >= 1) printf "+%.2f MB/s over last %ds", mbps, i;
           else           printf "+%.0f KB/s over last %ds", d/1024/i, i;
         }')
    elif (( delta == 0 )); then
      ollama_throughput="(no growth in last ${interval}s)"
    fi
  fi
fi
# Append current observation; trim to last 50 lines.
echo "$now_epoch $ollama_combined_bytes" >> "$state_file"
tail -50 "$state_file" > "$state_file.tmp" && mv "$state_file.tmp" "$state_file"

# ---------- MLX -------------------------------------------------------------
mlx_done=0
mlx_running=0
mlx_dir=""
mlx_safetensors=0

# Pick the model dir that's currently being written to by the MLX path
# (snapshot_download against an mlx-community repo), preferring the one with
# active .incomplete chunks. The HF-driven Ollama import path also writes
# under models/, so we MUST exclude that target here:
#  * if the wrapper is currently running, $hf_target points at it
#  * if the wrapper has finished, the dir still exists and contains a *.gguf
#    file but no MLX safetensors. We exclude any directory whose name does
#    NOT begin with "mlx-" so the two downloads can't shadow each other in
#    either state.
is_mlx_candidate() {
  local cand="$1"
  local name="${cand##*/}"
  [[ "$name" == mlx-* || "$name" == *_mlx_* || "$name" == *-mlx-* ]]
}
if [[ -d "$REPO_ROOT/models" ]]; then
  active=""
  for d in "$REPO_ROOT/models"/*/; do
    [[ -d "$d" ]] || continue
    cand="${d%/}"
    [[ -n "$hf_target" && "$cand" == "$hf_target" ]] && continue
    is_mlx_candidate "$cand" || continue
    if compgen -G "$d/.cache/huggingface/download/*.incomplete" >/dev/null \
       || compgen -G "$d/.cache/huggingface/download/*.lock" >/dev/null; then
      active="$cand"
      break
    fi
  done
  if [[ -n "$active" ]]; then
    mlx_dir="$active"
  else
    # Largest finished MLX-style tree.
    mlx_dir=$(
      for d in "$REPO_ROOT/models"/*/; do
        cand="${d%/}"
        [[ -n "$hf_target" && "$cand" == "$hf_target" ]] && continue
        is_mlx_candidate "$cand" || continue
        du -s "$cand" 2>/dev/null
      done | sort -rn | awk 'NR==1 {print $2}'
    )
  fi
fi

if [[ -n "$mlx_dir" ]]; then
  # A finished MLX repo has at least one *.safetensors file at the root and
  # no *.lock or *.incomplete files in .cache/huggingface/download.
  mlx_safetensors=$(find "$mlx_dir" -maxdepth 1 -name "*.safetensors" 2>/dev/null | wc -l | tr -d ' ')
  mlx_pending=0
  if [[ -d "$mlx_dir/.cache/huggingface/download" ]]; then
    mlx_pending=$(find "$mlx_dir/.cache/huggingface/download" -maxdepth 1 \( -name "*.lock" -o -name "*.incomplete" \) 2>/dev/null | wc -l | tr -d ' ')
  fi
  if (( mlx_safetensors > 0 )) && (( mlx_pending == 0 )); then
    mlx_done=1
  fi
fi

if pgrep -f "snapshot_download" >/dev/null 2>&1; then
  mlx_running=1
fi

mlx_disk=""
if [[ -n "$mlx_dir" ]]; then
  mlx_disk=$(du -sh "$mlx_dir" 2>/dev/null | awk '{print $1}')
fi

# ---------- Output ----------------------------------------------------------
printf "\n%sBackground download status%s  (%s)\n" "${GREEN}" "${RESET}" "$(date '+%H:%M:%S')"
printf "  %srepo:%s %s\n" "${DIM}" "${RESET}" "$REPO_ROOT"
printf "\n"

# Ollama
if (( ollama_done == 1 )); then
  printf "  Ollama:    ${GREEN}DONE${RESET}    %s on disk   (tag: %s)\n" \
    "${ollama_disk:-?}" "${ollama_tag_found}"
elif (( ollama_running == 1 )); then
  printf "  Ollama:    ${YELLOW}RUNNING${RESET} %s GB downloaded   pulling: %s\n" \
    "${ollama_alloc_gb:-?}" "${ollama_pull_tags:-?}"
  if [[ -n "$ollama_throughput" ]]; then
    printf "             ${DIM}rate: %s${RESET}\n" "$ollama_throughput"
  fi
  if [[ -n "$ollama_self_progress" ]]; then
    printf "             ${DIM}ollama: %s${RESET}\n" "$ollama_self_progress"
  fi
  if [[ -n "$ollama_blob_lines" ]]; then
    while IFS= read -r line; do
      printf "             ${DIM}blob %s${RESET}\n" "$line"
    done <<< "$ollama_blob_lines"
  fi
  if [[ -n "$ollama_hf_pids" && -n "$hf_target" ]]; then
    printf "             ${DIM}hf target: %s (%s GB)${RESET}\n" \
      "${hf_target##*/}" "$hf_target_gb"
    hf_watchdog=$(sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$REPO_ROOT/.logs/install-ollama-from-hf.log" 2>/dev/null \
      | grep -E "^\[watchdog\]" \
      | tail -1)
    if [[ -n "$hf_watchdog" ]]; then
      printf "             ${DIM}%s${RESET}\n" "$hf_watchdog"
    fi
  fi
else
  printf "  Ollama:    ${RED}STOPPED${RESET} %s GB downloaded   no \`ollama pull\` process and no model loaded\n" \
    "${ollama_alloc_gb:-?}"
  if [[ -n "$ollama_blob_lines" ]]; then
    while IFS= read -r line; do
      printf "             ${DIM}partial blob %s${RESET}\n" "$line"
    done <<< "$ollama_blob_lines"
  fi
  printf "             %sretry / resume: bash scripts/20-ollama.sh%s\n" "${DIM}" "${RESET}"
fi

# MLX
if (( mlx_done == 1 )); then
  printf "  MLX:       ${GREEN}DONE${RESET}    %s on disk   (%d safetensors in %s)\n" \
    "${mlx_disk:-?}" "$mlx_safetensors" "${mlx_dir##*/}"
elif (( mlx_running == 1 )); then
  printf "  MLX:       ${YELLOW}RUNNING${RESET} %s on disk   (%d/?? safetensors materialized)\n" \
    "${mlx_disk:-?}" "$mlx_safetensors"
elif [[ -n "$mlx_dir" ]]; then
  printf "  MLX:       ${RED}STOPPED${RESET} %s on disk   no snapshot_download process and download incomplete\n" \
    "${mlx_disk:-?}"
  printf "             %sretry: MLX_PREFER_NEXT=0 bash scripts/30-mlx.sh%s\n" "${DIM}" "${RESET}"
else
  printf "  MLX:       ${RED}MISSING${RESET} no models/ directory yet\n"
  printf "             %sretry: bash scripts/30-mlx.sh%s\n" "${DIM}" "${RESET}"
fi

printf "\n"

# ---------- Decide exit code + next-step nudge ------------------------------
if (( ollama_done == 1 )) && (( mlx_done == 1 )); then
  printf "  ${GREEN}Both downloads complete.${RESET} Run \`make finalize\` to start MLX and refresh services.\n\n"
  exit 0
fi

if (( ollama_running == 0 )) && (( ollama_done == 0 )); then exit 2; fi
if (( mlx_running == 0 )) && (( mlx_done == 0 )); then exit 2; fi

exit 1
