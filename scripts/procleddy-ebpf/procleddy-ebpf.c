// SPDX-License-Identifier: GPL-2.0
/* procleddy-ebpf — eBPF-backed process-launch watcher for the klangk
 * process-ledger (#2520 spike).
 *
 * Same wire contract as the /proc poller (scripts/procleddy/procleddy.c):
 * scope (workspace root host pids) arrives as NDJSON lines on stdin,
 * launch events leave as NDJSON lines on stdout. klangkd spawns whichever
 * binary KLANGKD_PROCESS_LEDGER_BACKEND selects; no Python changes are
 * needed beyond that selection.
 *
 * Requires CAP_BPF + CAP_PERFMON (or root) — the privileged deployment
 * tier. Loads the BPF object from the binary-adjacent
 * procleddy-ebpf.bpf.o.
 *
 * Event mapping: one NDJSON "birth" per execve (fork+exec merged — the
 * ledger's launch unit), "exec" for a re-exec of an already-seen pid.
 * Short-lived processes are captured exactly (no polling dark window):
 * the kernel-side hook fires at syscall entry regardless of lifetime.
 */

#define _GNU_SOURCE
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "procleddy_ebpf.h"

static volatile sig_atomic_t stopped = 0;
static int verbose = 0;
static int parents_fd = -1;
static int roots_fd = -1;

/* Recently emitted pids (ring): a repeat exec of a known pid is an "exec"
 * event, not another birth. */
#define SEEN_RING 4096
static uint32_t seen[SEEN_RING];
static size_t seen_next;

static void on_term(int sig) { (void)sig; stopped = 1; }

static double realtime_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void json_escape(const char *in, char *out, size_t outsz) {
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)in; *p && o + 7 < outsz; p++) {
        if (*p == '"' || *p == '\\') {
            out[o++] = '\\';
            out[o++] = (char)*p;
        } else if (*p < 0x20) {
            o += (size_t)snprintf(out + o, outsz - o, "\\u%04x", *p);
        } else {
            out[o++] = (char)*p;
        }
    }
    out[o] = '\0';
}

/* argv is NUL-packed (argc strings); flatten to a single escaped line. */
static void argv_to_line(const struct exec_event *ev, char *out, size_t outsz) {
    size_t o = 0;
    const char *p = ev->argv;
    out[0] = '\0';
    for (int i = 0; i < ev->argc && o + 8 < outsz; i++) {
        if (i) out[o++] = ' ';
        char esc[ARG_MAX * 2 + 8];
        json_escape(p, esc, sizeof esc);
        size_t n = strlen(esc);
        if (o + n >= outsz - 8) break;
        memcpy(out + o, esc, n);
        o += n;
        p += strlen(p) + 1;
    }
    out[o] = '\0';
}

static int mark_seen(uint32_t pid) {
    for (size_t i = 0; i < SEEN_RING; i++)
        if (seen[i] == pid)
            return 1;
    seen[seen_next] = pid;
    seen_next = (seen_next + 1) % SEEN_RING;
    return 0;
}

/* Finish an unknown-ancestry walk in userspace: keep hopping through the
 * parents map (same map the BPF side maintains), falling back to
 * /proc/<pid>/status PPid for pre-monitor forks. Returns 1 and fills
 * *ppid_out when a root is reached. */
static int finish_walk(uint32_t pid, uint32_t root_list[], size_t nroots,
                       uint32_t *ppid_out) {
    uint32_t cur = pid;
    for (int depth = 0; depth < 64; depth++) {
        uint32_t parent = 0;
        if (parents_fd >= 0 &&
            bpf_map_lookup_elem(parents_fd, &cur, &parent) != 0) {
            /* not in the kernel map: pre-monitor fork — /proc fallback */
            char path[64];
            snprintf(path, sizeof path, "/proc/%" PRIu32 "/status", cur);
            FILE *f = fopen(path, "r");
            if (!f) return 0;
            char line[256];
            while (fgets(line, sizeof line, f)) {
                if (strncmp(line, "PPid:", 5) == 0) {
                    parent = (uint32_t)strtoul(line + 5, NULL, 10);
                    break;
                }
            }
            fclose(f);
            if (parent == 0) return 0;
        }
        for (size_t i = 0; i < nroots; i++) {
            if (cur == root_list[i] || parent == root_list[i]) {
                if (ppid_out) *ppid_out = parent;
                return 1;
            }
        }
        if (parent <= 1 || parent == cur) return 0;
        cur = parent;
    }
    return 0;
}

static int on_rb_event(void *ctx, void *data, size_t size) {
    (void)ctx; (void)size;
    const struct exec_event *ev = data;

    static uint32_t root_list[MAX_ROOTS];
    static size_t nroots;
    nroots = 0;
    uint32_t next;
    uint32_t *prev_key = NULL;
    while (nroots < MAX_ROOTS &&
           bpf_map_get_next_key(roots_fd, prev_key, &next) == 0) {
        root_list[nroots++] = next;
        prev_key = &root_list[nroots - 1];
    }

    uint32_t ppid = ev->ppid;
    if (!ev->member) {
        if (!finish_walk(ev->pid, root_list, nroots, &ppid))
            return 0; /* confirmed outside every workspace */
    }

    char comm_esc[64], argv_line[ARGV_BUF * 2 + 16];
    json_escape(ev->comm, comm_esc, sizeof comm_esc);
    argv_to_line(ev, argv_line, sizeof argv_line);
    const char *kind = mark_seen(ev->pid) ? "exec" : "birth";
    printf("{\"type\":\"%s\",\"pid\":%" PRIu32 ",\"ppid\":%" PRIu32
           ",\"uid\":%" PRIu32 ",\"euid\":%" PRIu32 ",\"sid\":0"
           ",\"comm\":\"%s\",\"argv\":\"%s\",\"ancestry\":[%" PRIu32
           "],\"ts_monotonic\":%.6f,\"ts_realtime\":%.6f}\n",
           kind, ev->pid, ppid, ev->uid, ev->euid, comm_esc, argv_line,
           ppid, (double)ev->ts_ns / 1e9, realtime_s());
    fflush(stdout);
    return 0;
}

/* --- stdin scope pump (same contract as the /proc poller) --- */
static void replace_roots(const uint32_t *pids, size_t n) {
    uint32_t next;
    while (bpf_map_get_next_key(roots_fd, NULL, &next) == 0)
        bpf_map_delete_elem(roots_fd, &next);
    for (size_t i = 0; i < n && i < MAX_ROOTS; i++) {
        uint8_t one = 1;
        if (bpf_map_update_elem(roots_fd, &pids[i], &one, BPF_ANY) != 0 &&
            verbose)
            fprintf(stderr, "procleddy-ebpf: roots map update failed\n");
    }
}

static void read_scope_line(const char *line) {
    const char *p = strstr(line, "\"roots\"");
    if (!p) return;
    p = strchr(p + 7, '[');
    if (!p) return;
    static uint32_t pids[MAX_ROOTS];
    size_t n = 0;
    p++;
    while (*p && *p != ']' && n < MAX_ROOTS) {
        if (*p >= '0' && *p <= '9') {
            unsigned long v = 0;
            while (*p >= '0' && *p <= '9' &&
                   v <= 8u * 1024 * 1024) {
                v = v * 10 + (unsigned long)(*p - '0');
                p++;
            }
            pids[n++] = (uint32_t)v;
        } else {
            p++;
        }
    }
    replace_roots(pids, n);
    if (verbose)
        fprintf(stderr, "procleddy-ebpf: scope now %zu roots\n", n);
}

static void pump_stdin(void) {
    static char buf[16384];
    static size_t fill = 0;
    for (;;) {
        ssize_t r = read(STDIN_FILENO, buf + fill, sizeof buf - fill);
        if (r < 0) {
            if (errno == EINTR) continue;
            return;
        }
        if (r == 0) return; /* writer closed: keep the last scope */
        fill += (size_t)r;
        char *nl;
        while ((nl = memchr(buf, '\n', fill)) != NULL) {
            *nl = '\0';
            read_scope_line(buf);
            size_t consumed = (size_t)(nl - buf) + 1;
            memmove(buf, buf + consumed, fill - consumed);
            fill -= consumed;
        }
        if (fill == sizeof buf) fill = 0; /* oversized line: drop */
    }
}

static char *adjacent_object(const char *argv0) {
    static char path[4096];
    ssize_t n = readlink("/proc/self/exe", path, sizeof path - 32);
    if (n > 0) {
        path[n] = '\0';
        char *slash = strrchr(path, '/');
        if (slash) {
            snprintf(slash + 1, sizeof path - (size_t)(slash + 1 - path),
                     "procleddy-ebpf.bpf.o");
            if (access(path, R_OK) == 0)
                return path;
        }
    }
    if (strchr(argv0, '/')) {
        snprintf(path, sizeof path, "%s.bpf.o", argv0);
        if (access(path, R_OK) == 0)
            return path;
    }
    return NULL;
}

/* --- socket service mode -------------------------------------------------
 *
 * With --socket PATH the monitor detaches from klangkd's process tree and
 * runs as its own (privileged) systemd service: it listens on a UNIX
 * socket, clients (klangkd) connect and speak the same wire contract —
 * scope NDJSON lines in, event NDJSON lines out. Events and heartbeats are
 * broadcast to every connected client; scope lines are accepted from any
 * client (single-klangkd deployments; last scope wins).
 *
 * --dry-run skips all BPF work (no object, no load, no attach) and emits
 * heartbeats + scope acks only: it exercises the socket/stdio plumbing
 * everywhere — CI, smoke tests, unit debugging — without privileges.
 */

#define MAX_CLIENTS 8

static int listen_fd = -1;
static int clients[MAX_CLIENTS];
static char client_buf[MAX_CLIENTS][16384];
static size_t client_fill[MAX_CLIENTS];

static void broadcast(const char *line) {
    size_t len = strlen(line);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (clients[i] < 0)
            continue;
        ssize_t w = write(clients[i], line, len);
        (void)w; /* a slow/dead client is dropped on the next recv */
    }
}

static void emit_line(const char *fmt, ...) {
    char line[4096];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    size_t n = strlen(line);
    line[n] = '\n';
    line[n + 1] = '\0';
    if (listen_fd >= 0)
        broadcast(line);
    else {
        fputs(line, stdout);
        fputc('\n', stdout);
    }
}

static void pump_clients(void) {
    /* accept new connections */
    for (;;) {
        struct sockaddr_un addr;
        socklen_t alen = sizeof addr;
        int fd = accept(listen_fd, (struct sockaddr *)&addr, &alen);
        if (fd < 0)
            return; /* EAGAIN / nothing pending */
        int slot = -1;
        for (int i = 0; i < MAX_CLIENTS; i++)
            if (clients[i] < 0) {
                slot = i;
                break;
            }
        if (slot < 0) {
            close(fd); /* more klangkds than anyone planned for */
            continue;
        }
        int fl = fcntl(fd, F_GETFL, 0);
        if (fl >= 0)
            fcntl(fd, F_SETFL, fl | O_NONBLOCK);
        clients[slot] = fd;
        client_fill[slot] = 0;
        if (verbose)
            fprintf(stderr, "procleddy-ebpf: client %d connected\n", slot);
    }
    /* drain client input: scope lines */
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (clients[i] < 0)
            continue;
        ssize_t r = read(clients[i], client_buf[i] + client_fill[i],
                         sizeof client_buf[i] - client_fill[i]);
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        if (r <= 0) { /* EOF or error: drop the client */
            close(clients[i]);
            clients[i] = -1;
            continue;
        }
        client_fill[i] += (size_t)r;
        char *nl;
        while ((nl = memchr(client_buf[i], '\n', client_fill[i])) != NULL) {
            *nl = '\0';
            read_scope_line(client_buf[i]);
            size_t consumed = (size_t)(nl - client_buf[i]) + 1;
            memmove(client_buf[i], client_buf[i] + consumed,
                    client_fill[i] - consumed);
            client_fill[i] -= consumed;
        }
        if (client_fill[i] == sizeof client_buf[i])
            client_fill[i] = 0; /* oversized line: drop */
    }
}

static int open_socket(const char *path) {
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) {
        fprintf(stderr, "procleddy-ebpf: socket path too long\n");
        return -1;
    }
    strcpy(addr.sun_path, path);
    unlink(path); /* stale socket from a previous run */
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("procleddy-ebpf: socket");
        return -1;
    }
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        listen(fd, 8) < 0) {
        perror("procleddy-ebpf: bind/listen");
        close(fd);
        return -1;
    }
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl >= 0)
        fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    return fd;
}

int main(int argc, char **argv) {
    const char *sockpath = NULL;
    int dry_run = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--interval-ms") && i + 1 < argc) {
            i++; /* accepted for contract parity; capture is event-driven */
        } else if (!strcmp(argv[i], "--socket") && i + 1 < argc) {
            sockpath = argv[++i];
        } else if (!strcmp(argv[i], "--dry-run")) {
            dry_run = 1;
        } else if (!strcmp(argv[i], "-v")) {
            verbose = 1;
        }
    }
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    signal(SIGPIPE, SIG_IGN);
    for (int i = 0; i < MAX_CLIENTS; i++)
        clients[i] = -1;

    struct bpf_object *obj = NULL;
    struct ring_buffer *rb = NULL;
    if (!dry_run) {
        char *objpath =
            adjacent_object(argc ? argv[0] : "procleddy-ebpf");
        if (!objpath) {
            fprintf(stderr,
                    "procleddy-ebpf: cannot locate procleddy-ebpf.bpf.o\n");
            return 1;
        }
        obj = bpf_object__open_file(objpath, NULL);
        if (libbpf_get_error(obj)) {
            fprintf(stderr, "procleddy-ebpf: open %s failed\n", objpath);
            return 1;
        }
        if (bpf_object__load(obj)) {
            fprintf(stderr,
                    "procleddy-ebpf: BPF load failed (%s) — this backend "
                    "needs CAP_BPF + CAP_PERFMON\n",
                    strerror(errno));
            return 1;
        }
        struct bpf_program *prog;
        const char *names[] = {"on_fork", "on_exit", "on_execve",
                               "on_execveat"};
        for (size_t i = 0; i < sizeof names / sizeof *names; i++) {
            prog = bpf_object__find_program_by_name(obj, names[i]);
            if (!prog) {
                fprintf(stderr, "procleddy-ebpf: program %s missing\n",
                        names[i]);
                return 1;
            }
            struct bpf_link *link = bpf_program__attach(prog);
            if (libbpf_get_error(link)) {
                fprintf(stderr,
                        "procleddy-ebpf: attach %s failed (%s) — tracefs "
                        "must be readable (see docs)\n",
                        names[i], strerror(errno));
                return 1;
            }
        }
        parents_fd = bpf_object__find_map_fd_by_name(obj, "parents");
        roots_fd = bpf_object__find_map_fd_by_name(obj, "roots");
        int events_fd = bpf_object__find_map_fd_by_name(obj, "events");
        if (parents_fd < 0 || roots_fd < 0 || events_fd < 0) {
            fprintf(stderr, "procleddy-ebpf: map fd missing\n");
            return 1;
        }
        rb = ring_buffer__new(events_fd, on_rb_event, NULL, NULL);
        if (!rb) {
            fprintf(stderr, "procleddy-ebpf: ringbuf alloc failed\n");
            return 1;
        }
    } else if (verbose) {
        fprintf(stderr, "procleddy-ebpf: dry-run (no BPF)\n");
    }

    if (sockpath) {
        listen_fd = open_socket(sockpath);
        if (listen_fd < 0)
            return 1;
        emit_line("{\"type\":\"snapshot_start\",\"ts\":%.6f}",
                  realtime_s());
        emit_line("{\"type\":\"snapshot_end\",\"ts\":%.6f}", realtime_s());
    } else {
        int fl = fcntl(STDIN_FILENO, F_GETFL, 0);
        if (fl >= 0)
            fcntl(STDIN_FILENO, F_SETFL, fl | O_NONBLOCK);
    }

    uint64_t beats = 0;
    struct timespec last = {0, 0};
    clock_gettime(CLOCK_MONOTONIC, &last);
    while (!stopped) {
        if (rb)
            ring_buffer__poll(rb, 50);
        else
            usleep(50000); /* dry-run pacing */
        if (listen_fd >= 0)
            pump_clients();
        else
            pump_stdin();
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec - last.tv_sec >= 1) {
            /* Event-driven capture: interval 0 conveys "no polling". */
            emit_line("{\"type\":\"heartbeat\",\"polls\":%llu,"
                      "\"listed\":0,\"roots\":0,\"poll_ms\":0.0,"
                      "\"interval_ms\":0.0}",
                      (unsigned long long)++beats);
            last = now;
        }
    }
    emit_line("{\"type\":\"snapshot_end\",\"ts\":%.6f}", realtime_s());
    if (listen_fd >= 0 && sockpath)
        unlink(sockpath);
    if (rb)
        ring_buffer__free(rb);
    if (obj)
        bpf_object__close(obj);
    return 0;
}
