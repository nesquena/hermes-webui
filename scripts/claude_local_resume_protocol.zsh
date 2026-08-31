#!/bin/zsh

# Deployment helper for claude-local. Task 6 wires this into the live wrapper.
emulate -L zsh
setopt errexit nounset pipefail
umask 077

if (( $# < 3 )); then
  exit 2
fi

readonly task_config_dir="$1"
readonly task_store_id="$2"
readonly task_command="$3"
shift 3
task_args=("$@")

task_is_uuid() {
  [[ "$1" =~ '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' ]]
}

task_lease_held=0
for (( task_index = 1; task_index <= ${#task_args}; ++task_index )); do
  if [[ "${task_args[task_index]}" == "--hermes-lease-held" ]] \
    && [[ "${task_args[task_index + 1]:-}" == "--resume" ]] \
    && task_is_uuid "${task_args[task_index + 2]:-}"; then
    task_args[task_index]=()
    task_lease_held=1
    break
  fi
done

task_resume_uuid=""
for (( task_index = 1; task_index <= ${#task_args}; ++task_index )); do
  task_argument="${task_args[task_index]}"
  case "${task_argument}" in
    --resume|-r)
      if task_is_uuid "${task_args[task_index + 1]:-}"; then
        task_resume_uuid="${task_args[task_index + 1]}"
      fi
      ;;
    --resume=*|-r=*)
      task_candidate="${task_argument#*=}"
      if task_is_uuid "${task_candidate}"; then
        task_resume_uuid="${task_candidate}"
      fi
      ;;
  esac
  if [[ -n "${task_resume_uuid}" ]]; then
    break
  fi
done

if (( task_lease_held )) || [[ -z "${task_resume_uuid}" ]]; then
  exec "${task_command}" "${task_args[@]}"
fi

task_resume_uuid="${(L)task_resume_uuid}"
task_store_hash=$(/usr/bin/printf '%s' "${task_store_id}" | /usr/bin/shasum -a 256)
task_store_hash="${task_store_hash%% *}"
task_store_hash="${task_store_hash[1,16]}"
readonly task_lock_dir="${task_config_dir}/.hermes-resume-locks"
readonly task_lock_file="${task_lock_dir}/${task_store_hash}-${task_resume_uuid}.lock"

/bin/mkdir -p -m 700 -- "${task_lock_dir}"
readonly task_uid=$(/usr/bin/id -u)
if [[ -L "${task_lock_dir}" ]] \
  || [[ ! -d "${task_lock_dir}" ]] \
  || [[ "$(/usr/bin/stat -f '%u:%Lp' "${task_lock_dir}")" != "${task_uid}:700" ]]; then
  exit 1
fi
if [[ -e "${task_lock_file}" || -L "${task_lock_file}" ]]; then
  if [[ -L "${task_lock_file}" ]] \
    || [[ ! -f "${task_lock_file}" ]] \
    || [[ "$(/usr/bin/stat -f '%u:%l' "${task_lock_file}")" != "${task_uid}:1" ]]; then
    exit 1
  fi
fi
/usr/bin/touch -- "${task_lock_file}"
if [[ -L "${task_lock_file}" ]] \
  || [[ ! -f "${task_lock_file}" ]] \
  || [[ "$(/usr/bin/stat -f '%u:%l' "${task_lock_file}")" != "${task_uid}:1" ]]; then
  exit 1
fi

exec /usr/bin/lockf -kn "${task_lock_file}" "${task_command}" "${task_args[@]}"
