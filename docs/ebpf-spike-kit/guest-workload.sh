#!/bin/bash
# Runs AS THE LIMA USER (the uid the dev box runs as) in the guest. Each step prints a marker the
# analysis keys on; the container ids it prints are the ground truth the events are judged against.
set -u
SOCK=/run/user/$(id -u)/podman/podman.sock
RUN=${FY_SPIKE_RUN:-$(date +%s)}
img_box=docker.io/curlimages/curl:8.16.0
img_alp=docker.io/library/alpine:3.22
podman pull -q $img_box $img_alp >/dev/null

mark() { echo "MARK $(date -u +%FT%T.%NZ) $*"; }

# 1. "the box": holds the engine socket, run the way box.py runs it (--user 0, label=disable).
podman run -d --name box-$RUN --user 0 --security-opt label=disable \
    -v "$SOCK":/var/run/docker.sock $img_box sleep 3600 >/dev/null
mark box "$(podman inspect -f '{{.Id}}' box-$RUN)"

# 2. native activity inside the box: file read, file write+read, a direct (proxy-ignoring) connect.
mark box-activity-start
podman exec box-$RUN sh -c 'cat /etc/passwd >/dev/null; echo hi >/tmp/fy-secret; cat /tmp/fy-secret >/dev/null;
    timeout 5 curl -sS --noproxy "*" -o /dev/null https://1.1.1.1 2>/dev/null; true'
mark box-activity-end

# 3. an agent-created sibling, through the engine socket from INSIDE the box (docker-compat API).
printf '%s' "{\"Image\":\"$img_alp\",\"Cmd\":[\"sh\",\"-c\",\"sleep 2; cat /etc/passwd >/dev/null; sleep 600\"]}" \
    >/tmp/create-$RUN.json
podman cp /tmp/create-$RUN.json box-$RUN:/tmp/create.json
podman exec box-$RUN sh -c "curl -sS --unix-socket /var/run/docker.sock -H 'Content-Type: application/json' \
    -d @/tmp/create.json 'http://d/v1.41/containers/create?name=sib-$RUN' >/dev/null &&
    curl -sS -X POST --unix-socket /var/run/docker.sock http://d/v1.41/containers/sib-$RUN/start"
sleep 5
mark sibling "$(podman inspect -f '{{.Id}}' sib-$RUN)"

# 4. the same payload with NO sub-cgroup (crun's run.oci.systemd.subgroup annotation emptied) …
podman run -d --name flat-$RUN --annotation run.oci.systemd.subgroup= $img_alp \
    sh -c 'sleep 2; cat /etc/passwd >/dev/null; sleep 600' >/dev/null
# 5. … and with the sub-cgroup NAMED AFTER THE BOX: whose activity does the collector report?
podman run -d --name forge-$RUN --annotation "run.oci.systemd.subgroup=$(podman inspect -f '{{.Id}}' box-$RUN)" \
    $img_alp sh -c 'sleep 2; cat /etc/passwd >/dev/null; sleep 600' >/dev/null
sleep 5
mark flat "$(podman inspect -f '{{.Id}}' flat-$RUN)"
mark forge "$(podman inspect -f '{{.Id}}' forge-$RUN)"
for c in box sib flat forge; do
    pid=$(podman inspect -f '{{.State.Pid}}' $c-$RUN)
    echo "CGROUP $c $(cat /proc/$pid/cgroup)"
done

# 6. churn, for event volume: 500 execs in the box.
mark churn-start
podman exec box-$RUN sh -c 'i=0; while [ $i -lt 500 ]; do /bin/true; i=$((i+1)); done'
mark churn-end

# 7. the monitor's surface as seen by this uid and from inside the box.
echo "SURFACE socket: $(tetra status 2>&1 | grep -o 'permission denied' | head -1)"
echo "SURFACE log: $(cat /var/log/tetragon/tetragon.log 2>&1 >/dev/null | grep -o 'Permission denied')"
echo "SURFACE stop: $(systemctl stop tetragon 2>&1 | grep -o 'Access denied')"
echo "SURFACE health-from-box: $(podman exec box-$RUN sh -c 'timeout 4 curl -s --noproxy "*" -o /dev/null \
    -w "%{http_code}" --http2-prior-knowledge http://host.containers.internal:6789/' 2>/dev/null)"
mark done
