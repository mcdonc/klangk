/* procleddy — the /proc watcher for the klangk process-launch ledger (#2520).
 *
 * One job: turn /proc into an ordered NDJSON event stream on stdout.
 * Scope (workspace root pids, one JSON object {"type":"scope","roots":[...]}
 * per line) arrives on stdin. Never learns what a workspace or an agent is.
 *
 * Event contract (#2520):
 *   birth        pid, ppid captured at first sight, sid, uid+euid, comm,
 *                argv, ancestry chain, ts — the attribution backbone,
 *                recorded before a daemonize double-fork reparents it away
 *   exec         comm changed on a known watched pid (shells, sudo,
 *                re-exec); argv re-read on emit
 *   exit         watched pid unseen for K consecutive polls
 *   reparent     ppid changed on a live watched pid (daemonization
 *                fingerprint: fork/setsid/fork shows as birth+reparent)
 *   euid_change  effective-uid transition on a live watched pid (the
 *                boundary-eraser alarm)
 *   heartbeat    ~1/s: seq, polls, watched count, roots, poll_ms,
 *                interval_ms — lets the consumer prove coverage + see the
 *                duty-cycle governor stretching the cadence
 *   snapshot_start / snapshot_end   bracket the cold-start scan (v1 emits
 *                them around nothing; events before end are pre-existing)
 *
 * Performance contract: 80 ms interval at <=1% of one core at ~12k host
 * pids. The trick that buys it: status is parsed at full rate ONLY for
 * (a) pids new since the previous poll (birth detection + ancestry) and
 * (b) watched pids (the workspace subtrees — small). Everyone else gets
 * a STAGGERED refresh (each pid refreshed once per REFRESH_MOD polls,
 * ~20 s) so ancestry walks for new pids still resolve. Parsing all pids
 * every poll would be ~5-10 ms at 1.5k pids — over budget by itself.
 *
 * Robustness threat model (#2520 adversarial review): /proc content is
 * attacker-influenced (a hostile workspace cannot write /proc directly,
 * but the ledger is also pointed at fake trees in tests, and a future
 * --root could be a FUSE mount an attacker controls). Every parse is
 * therefore bounded and total:
 *   - getdents64 records: d_reclen validated (>= 8, within the buffer)
 *     so a malformed record can neither overrun nor spin the loop;
 *     d_name is length-bounded and manually digit-checked (never passed
 *     to strtoul on unterminated memory).
 *   - status/cmdline reads: fixed buffers, short/oversized/garbage reads
 *     degrade to "no event this poll", never to unbounded parsing.
 *   - pids capped at MAX_PIDS (1<<20): a pid above the cap is ignored
 *     (bounded memory; Linux pid_max default is well under this).
 *   - ancestry walks: depth-capped + stamp-visited (a corrupted ppid
 *     cycle cannot loop forever).
 *   - printf JSON strings escaped + field-capped.
 *
 * Duty-cycle governor: if a poll's work exceeds the interval budget the
 * cadence stretches (sleep >= 1 ms, never spin); the heartbeat reports
 * the effective interval so degraded coverage is visible, never silent.
 *
 * Build: cc -O2 -Wall -Wextra -o procleddy procleddy.c
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_ROOT "/proc"
#define MAX_PIDS (1 << 20)   /* hard pid cap: bounds every table */
#define EXIT_POLLS 3         /* unseen-polls before exit event */
#define ANCESTRY_MAX 64      /* chain length carried on birth */
#define WALK_DEPTH 1024      /* hard cap for ppid-chain walks */
#define STATUS_BUF 8192
#define CMDLINE_BUF 4096
#define DIRENT_BUF (512 * 1024)
#define MAX_ROOTS 4096       /* sane cap on workspace root count */

struct linux_dirent64 {
    uint64_t d_ino;
    int64_t d_off;
    unsigned short d_reclen;
    unsigned char d_type;
    char d_name[];
};

struct pinfo {
    uint32_t ppid;
    uint32_t uid;
    uint32_t euid;
    uint32_t sid;
    uint64_t seen_poll;  /* poll index that last parsed this pid */
    uint64_t seen_walk;  /* ancestry-walk cycle stamp */
    uint16_t unseen;     /* consecutive polls not seen in a listing */
    uint8_t alive;       /* parsed at least once, not yet exited */
    uint8_t watched;     /* in a workspace subtree (full-rate parsing) */
    char comm[24];
};

/* Compact list of watched pids (the workspace subtrees) so the exit scan
 * is O(watched), not O(pid_cap). */
static uint32_t *watched_list;
static size_t nwatched, watched_cap;

static void watched_add(uint32_t pid) {
    if (nwatched >= MAX_PIDS) {
        /* More watched pids than the pid table can hold; this is a fork
         * bomb or a bug. Degrade: the pid stays un-watched (no change
         * detection), which is the safe direction. */
        return;
    }
    if (nwatched == watched_cap) {
        size_t want = watched_cap ? watched_cap * 2 : 256;
        if (want > MAX_PIDS) want = MAX_PIDS;
        uint32_t *nw = realloc(watched_list, want * sizeof *nw);
        if (!nw) {
            fprintf(stderr, "procleddy: realloc watched failed"
                    " — pid %" PRIu32 " will not be watched\n", pid);
            return;
        }
        watched_list = nw;
        watched_cap = want;
    }
    watched_list[nwatched++] = pid;
}

static struct pinfo *tab;          /* indexed by pid */
static uint32_t pid_cap = 0;       /* allocated slots (grows to MAX_PIDS) */
static const char *proc_root = DEFAULT_ROOT;
static int rootfd = -1;
static int verbose = 0;
static volatile sig_atomic_t stopped = 0;
static uint64_t poll_index = 0;  /* wide: no wraparound at 80ms polls */

static uint32_t *roots;
static size_t nroots, roots_cap;
static int scope_changed = 1; /* rescan membership on first/changed scope */

static void on_term(int sig) { (void)sig; stopped = 1; }

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static double realtime_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static int oom_logged = 0;

static void ensure_pid_table(uint32_t pid) {
    if (pid < pid_cap) return;
    uint32_t want = pid + 1024;
    if (want > MAX_PIDS) want = MAX_PIDS;
    struct pinfo *nt = calloc(want, sizeof *nt);
    if (!nt) {
        /* Out of memory: refuse to die — keep the old table and let the
         * caller skip the pid. Log once so the condition is diagnosable. */
        if (!oom_logged) {
            fprintf(stderr, "procleddy: calloc failed for pid %" PRIu32
                    " (want %" PRIu32 " slots) — pid will be invisible\n",
                    pid, want);
            oom_logged = 1;
        }
        return;
    }
    if (tab) {
        uint32_t copy = pid_cap < want ? pid_cap : want;
        memcpy(nt, tab, copy * sizeof *nt);
        free(tab);
    }
    tab = nt;
    pid_cap = want;
}

/* ------------------------------------------------------------------ */
/* scope (stdin)                                                       */

static void read_scope_line(const char *line) {
    /* Minimal parse of {"type":"scope","roots":[N,...]} — enough for our
     * own writer (klangk.process_ledger). Integers only; garbage between
     * digits is skipped; no allocation beyond roots growth. */
    const char *p = strstr(line, "\"roots\"");
    if (!p) return;
    p = strchr(p + 7, '[');
    if (!p) return;
    nroots = 0;
    p++;
    while (*p && *p != ']') {
        if (isdigit((unsigned char)*p)) {
            unsigned long v = 0;
            while (isdigit((unsigned char)*p)) {
                v = v * 10 + (unsigned long)(*p - '0');
                if (v > 4u * 1024 * 1024) v = 4u * 1024 * 1024;
                p++;
            }
            if (nroots >= MAX_ROOTS) {
                /* More roots than workspaces can plausibly exist;
                 * stop parsing — bounded memory beats unbounded. */
                break;
            }
            if (nroots == roots_cap) {
                size_t want = roots_cap ? roots_cap * 2 : 64;
                if (want > MAX_ROOTS) want = MAX_ROOTS;
                uint32_t *nr = realloc(roots, want * sizeof *nr);
                if (!nr) {
                    fprintf(stderr, "procleddy: realloc roots failed"
                            " — keeping %zu roots\n", nroots);
                    break;
                }
                roots = nr;
                roots_cap = want;
            }
            roots[nroots++] = (uint32_t)v;
        } else {
            p++;
        }
    }
    scope_changed = 1;
    if (verbose) fprintf(stderr, "procleddy: scope now %zu roots\n", nroots);
}

static void pump_stdin(void) {
    static char buf[16384];
    static size_t fill = 0;
    for (;;) {
        ssize_t r = read(STDIN_FILENO, buf + fill, sizeof buf - fill);
        if (r < 0) {
            if (errno == EINTR) continue;
            return; /* EAGAIN (nothing to read) or error */
        }
        if (r == 0) {
            /* EOF: writer closed. Keep the last scope; an empty scope
             * (no roots) stops all event emission, which is the safe
             * direction. */
            return;
        }
        fill += (size_t)r;
        char *nl;
        while ((nl = memchr(buf, '\n', fill)) != NULL) {
            *nl = '\0';
            read_scope_line(buf);
            size_t consumed = (size_t)(nl - buf) + 1;
            memmove(buf, buf + consumed, fill - consumed);
            fill -= consumed;
        }
        if (fill == sizeof buf) {
            fprintf(stderr, "procleddy: stdin line exceeded %zu bytes"
                    " — dropping buffer\n", sizeof buf);
            fill = 0;
        }
    }
}

static int is_root(uint32_t pid) {
    for (size_t i = 0; i < nroots; i++)
        if (roots[i] == pid) return 1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* /proc helpers — every parse bounded, every failure a clean skip     */

static int open_root(const char *path) {
    rootfd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    return rootfd;
}

/* List numeric dir entries into out[0..cap). Returns count or -1. */
static ssize_t list_proc(uint32_t *out, size_t cap) {
    static char buf[DIRENT_BUF];
    size_t n = 0;
    if (lseek(rootfd, 0, SEEK_SET) < 0) return -1;
    for (;;) {
        long r = syscall(SYS_getdents64, rootfd, buf, sizeof buf);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) break;
        size_t off = 0;
        while (off < (size_t)r) {
            struct linux_dirent64 *d = (struct linux_dirent64 *)(buf + off);
            unsigned rl = d->d_reclen;
            /* Hardened: a reclen that cannot advance, or that walks past
             * the kernel's byte count, ends the listing (defensive vs.
             * corrupted/attacker-influenced dirent data — the kernel
             * produces sane values, but we do not trust them). */
            if (rl < 8 || off + rl > (size_t)r) return (ssize_t)n;
            if (rl <= offsetof(struct linux_dirent64, d_name)) {
                off += rl;
                continue;
            }
            const char *nm = d->d_name;
            size_t nm_len = rl - offsetof(struct linux_dirent64, d_name) - 1;
            int ok = nm_len > 0;
            uint32_t v = 0;
            for (size_t i = 0; ok && i < nm_len && nm[i]; i++) {
                if (!isdigit((unsigned char)nm[i])) ok = 0;
                else if (v <= MAX_PIDS) v = v * 10 + (uint32_t)(nm[i] - '0');
            }
            if (ok && v > 0 && v < MAX_PIDS && n < cap) out[n++] = v;
            off += rl;
        }
    }
    return (ssize_t)n;
}

/* Parse PPid/Uid/NSsid/Name from one status read into *pi (comm/parent
 * fields only). Returns 0 on any failure — caller treats as a miss. */
static int parse_status(uint32_t pid, struct pinfo *pi) {
    char path[64], buf[STATUS_BUF];
    snprintf(path, sizeof path, "%" PRIu32 "/status", pid);
    int fd = openat(rootfd, path, O_RDONLY);
    if (fd < 0) return 0;
    ssize_t r = read(fd, buf, sizeof buf - 1);
    int saved = errno;
    close(fd);
    errno = saved;
    if (r <= 0 || (size_t)r >= sizeof buf - 1) return 0; /* empty/oversized */
    buf[r] = '\0';
    uint32_t ppid = 0, uid = 0, euid = 0, sid = 0;
    char name[24] = {0};
    int have_uid = 0;
    char *save = NULL;
    for (char *line = strtok_r(buf, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        if (line[0] == 'N' && !strncmp(line, "Name:", 5)) {
            /* bounded copy: at most sizeof(name)-1 chars */
            char *s = line + 5;
            while (*s == ' ' || *s == '\t') s++;
            size_t i = 0;
            while (s[i] && s[i] != '\n' && s[i] != '\r' && i < sizeof name - 1) {
                name[i] = s[i];
                i++;
            }
            name[i] = '\0';
        } else if (line[0] == 'P' && !strncmp(line, "PPid:", 5)) {
            char *s = line + 5;
            while (*s == ' ' || *s == '\t') s++;
            unsigned long pv = 0;
            while (isdigit((unsigned char)*s) && pv <= MAX_PIDS) {
                pv = pv * 10 + (unsigned long)(*s - '0');
                s++;
            }
            ppid = (uint32_t)(pv <= MAX_PIDS ? pv : 0);
        } else if (line[0] == 'U' && !strncmp(line, "Uid:", 4)) {
            /* Bounded manual parse: "Uid:\treal eff saved fs" — we need
             * real (a) and effective (b). Consistent with PPid/NSsid. */
            char *s = line + 4;
            while (*s == ' ' || *s == '\t') s++;
            unsigned long a = 0;
            while (isdigit((unsigned char)*s) && a <= 0xFFFFFFFFUL) {
                a = a * 10 + (unsigned long)(*s - '0');
                s++;
            }
            while (*s == ' ' || *s == '\t') s++;
            unsigned long b = 0;
            while (isdigit((unsigned char)*s) && b <= 0xFFFFFFFFUL) {
                b = b * 10 + (unsigned long)(*s - '0');
                s++;
            }
            uid = (uint32_t)(a <= 0xFFFFFFFFUL ? a : 0);
            euid = (uint32_t)(b <= 0xFFFFFFFFUL ? b : 0);
            have_uid = 1;
        } else if (line[0] == 'N' && !strncmp(line, "NSsid:", 6)) {
            char *s = line + 6;
            while (*s == ' ' || *s == '\t') s++;
            unsigned long sv = 0;
            while (isdigit((unsigned char)*s) && sv <= MAX_PIDS) {
                sv = sv * 10 + (unsigned long)(*s - '0');
                s++;
            }
            sid = (uint32_t)(sv <= MAX_PIDS ? sv : 0);
        }
    }
    if (!have_uid) return 0;
    pi->ppid = ppid;
    pi->uid = uid;
    pi->euid = euid;
    pi->sid = sid;
    memcpy(pi->comm, name, sizeof pi->comm);
    return 1;
}

static int read_cmdline(uint32_t pid, char *out, size_t cap) {
    char path[64];
    snprintf(path, sizeof path, "%" PRIu32 "/cmdline", pid);
    int fd = openat(rootfd, path, O_RDONLY);
    if (fd < 0) return 0;
    ssize_t r = read(fd, out, cap - 1);
    int saved = errno;
    close(fd);
    errno = saved;
    if (r <= 0) return 0;
    out[r] = '\0';
    /* NUL-separated -> spaces; strip trailing padding */
    for (ssize_t i = 0; i < r; i++)
        if (out[i] == '\0') out[i] = ' ';
    while (r > 0 && out[r - 1] == ' ') out[--r] = '\0';
    return 1;
}

static void json_escape(const char *in, char *out, size_t cap) {
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)in;
         *p && o + 6 < cap; p++) {
        if (*p == '"' || *p == '\\') {
            out[o++] = '\\';
            out[o++] = (char)*p;
        } else if (*p < 0x20 || *p == 0x7f) {
            o += (size_t)snprintf(out + o, cap - o, "\\u%04x", *p);
        } else {
            out[o++] = (char)*p;
        }
    }
    out[o] = '\0';
}

/* ------------------------------------------------------------------ */
/* ancestry                                                            */

/* Walk ppid chain from pid toward a root. Fills ancestry[0..alen)
 * nearest-ancestor-first, root-exclusive. Returns 1 if a root is an
 * ancestor (or pid IS a root, alen=0). Depth- and cycle-capped: uses the
 * per-poll seen stamp so a corrupted ppid cycle terminates. */
static uint64_t walk_epoch = 0;

static int descends_from_root(uint32_t pid, uint32_t *ancestry,
                              size_t *alen) {
    uint64_t stamp = ++walk_epoch;
    *alen = 0;
    uint32_t cur = pid;
    if (is_root(cur)) return 1;
    for (int depth = 0; depth < WALK_DEPTH; depth++) {
        if (cur == 0 || cur >= pid_cap || !tab[cur].alive) return 0;
        if (tab[cur].seen_walk == stamp) return 0; /* cycle */
        tab[cur].seen_walk = stamp;
        uint32_t pp = tab[cur].ppid;
        if (pp == 0 || pp == cur || pp >= MAX_PIDS) return 0;
        if (is_root(pp)) {
            /* include BOTH the root's child and the root itself: the
             * ledger joins workspaces + anchors on the chain, and the
             * root (container init) / its direct children (pane shells)
             * are exactly the anchor pids. */
            if (ancestry && *alen < ANCESTRY_MAX) ancestry[*alen] = cur;
            (*alen)++;
            if (ancestry && *alen < ANCESTRY_MAX) ancestry[*alen] = pp;
            (*alen)++;
            return 1;
        }
        if (ancestry && *alen < ANCESTRY_MAX) ancestry[*alen] = cur;
        (*alen)++;
        cur = pp;
    }
    return 0;
}

/* ------------------------------------------------------------------ */

/* Per-poll candidate buffer (phase A -> phase B). Bounded: a fork bomb
 * larger than this in one poll degrades to deferred classification. */
#define CAND_MAX 65536
static struct cand {
    uint32_t pid;
    struct pinfo old, fresh;
    int is_new;
} *cand;

int main(int argc, char **argv) {
    long interval_ms = 80;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--interval-ms") && i + 1 < argc) {
            const char *s = argv[++i];
            long v = 0;
            while (*s >= '0' && *s <= '9' && v <= 100000) {
                v = v * 10 + (*s - '0');
                s++;
            }
            interval_ms = v;
        } else if (!strcmp(argv[i], "--root") && i + 1 < argc) {
            proc_root = argv[++i];
        } else if (!strcmp(argv[i], "-v")) {
            verbose = 1;
        }
    }
    if (interval_ms < 10) interval_ms = 10;

    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    signal(SIGPIPE, SIG_IGN);

    if (open_root(proc_root) < 0) {
        perror("procleddy: open root");
        return 1;
    }
    int fl = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (fl >= 0) fcntl(STDIN_FILENO, F_SETFL, fl | O_NONBLOCK);

    /* Staggered-refresh modulus: non-watched pids are re-parsed once per
     * ~20 s (keeps ancestry fresh for new-pid walks at bounded cost). */
    uint64_t refresh_mod = (uint64_t)(20000 / interval_ms);
    if (refresh_mod < 1) refresh_mod = 1;

    uint32_t *listed = malloc(MAX_PIDS * sizeof *listed);
    if (!listed) { perror("malloc listed"); return 1; }
    cand = malloc(CAND_MAX * sizeof *cand);
    if (!cand) { perror("malloc cand"); return 1; }

    double budget = (double)interval_ms / 1000.0;
    double last_beat = now_s();
    uint64_t polls_since_beat = 0;
    static char argvbuf[CMDLINE_BUF], esc[CMDLINE_BUF * 2 + 8];
    char esc_comm[128];
    uint32_t ancestry[ANCESTRY_MAX];

    while (!stopped) {
        double t0 = now_s();
        poll_index++;

        pump_stdin();

        ssize_t n = list_proc(listed, MAX_PIDS);
        if (n < 0) {
            /* /proc listing failed entirely: emit nothing, sleep, retry.
             * A transient failure must not spin or die. */
            goto sleep;
        }

        /* Phase 0: grow the table for every listed pid. */
        for (ssize_t i = 0; i < n; i++)
            ensure_pid_table(listed[i]);

        /* Phase A (parse + store): full rate for new + watched pids,
         * staggered refresh for the rest (ancestry freshness). Everything
         * is STORED before any event logic runs so membership walks in
         * phase B see a consistent table regardless of directory order
         * (parent-after-child no longer loses the child). */
        size_t ncand = 0;
        for (ssize_t i = 0; i < n; i++) {
            uint32_t pid = listed[i];
            if (pid >= pid_cap) continue; /* table growth refused (OOM) */
            struct pinfo *pi = &tab[pid];
            int is_new = !pi->alive ||
                         (scope_changed && pi->alive && !pi->watched);
            int due_refresh =
                pi->alive && !pi->watched &&
                (poll_index - pi->seen_poll) >= refresh_mod;
            if (!is_new && !pi->watched && !due_refresh)
                continue;
            struct pinfo old = *pi;
            struct pinfo fresh = *pi;
            if (!parse_status(pid, &fresh)) {
                /* Raced away or unreadable: pass 3's seen_poll check
                 * accounts for it (single owner of the unseen count);
                 * staggered refresh retries later for non-watched. */
                continue;
            }
            fresh.seen_poll = poll_index;
            fresh.unseen = 0;
            int was_new = !pi->alive;
            fresh.alive = 1;
            if (was_new || (scope_changed && !pi->watched))
                fresh.watched = 0; /* determined in phase B */
            tab[pid] = fresh;
            if (ncand < CAND_MAX) {
                cand[ncand].pid = pid;
                cand[ncand].old = old;
                cand[ncand].fresh = fresh;
                cand[ncand].is_new = was_new ||
                                     (scope_changed && !old.watched);
                ncand++;
            }
            /* CAND overflow (fork bomb at boot): candidate processed
             * stored-but-unclassified; membership lands on the next
             * staggered refresh. Bounded memory beats unbounded. */
        }

        /* Phase B (emit): membership + change events against the fully
         * updated table. A scope change re-runs membership for every
         * known non-watched pid (bracketed by snapshot markers). */
        int rescanning = scope_changed;
        if (rescanning)
            printf("{\"type\":\"snapshot_start\",\"ts\":%.6f}\n",
                   realtime_s());
        for (size_t c = 0; c < ncand; c++) {
            uint32_t pid = cand[c].pid;
            struct pinfo *pi = &tab[pid];
            if (cand[c].is_new) {
                size_t alen = 0;
                int in_ws =
                    nroots > 0 && (is_root(pid) ||
                                   descends_from_root(pid, ancestry, &alen));
                if (!in_ws) continue;
                int has_argv = read_cmdline(pid, argvbuf, sizeof argvbuf);
                json_escape(has_argv ? argvbuf : cand[c].fresh.comm, esc,
                            sizeof esc);
                json_escape(cand[c].fresh.comm, esc_comm, sizeof esc_comm);
                printf("{\"type\":\"birth\",\"pid\":%" PRIu32
                       ",\"ppid\":%" PRIu32 ",\"uid\":%" PRIu32
                       ",\"euid\":%" PRIu32 ",\"sid\":%" PRIu32
                       ",\"comm\":\"%s\",\"argv\":\"%s\",\"ancestry\":[",
                       pid, cand[c].fresh.ppid, cand[c].fresh.uid,
                       cand[c].fresh.euid, cand[c].fresh.sid, esc_comm, esc);
                for (size_t k = 0; k < alen; k++)
                    printf("%s%" PRIu32, k ? "," : "", ancestry[k]);
                printf("],\"ts_monotonic\":%.6f,\"ts_realtime\":%.6f}\n",
                       t0, realtime_s());
                pi->watched = 1;
                watched_add(pid);
                continue;
            }
            if (!pi->watched) continue;
            struct pinfo *old = &cand[c].old;
            struct pinfo *fresh = &cand[c].fresh;
            if (strcmp(old->comm, fresh->comm) != 0) {
                int has_argv = read_cmdline(pid, argvbuf, sizeof argvbuf);
                json_escape(has_argv ? argvbuf : fresh->comm, esc,
                            sizeof esc);
                json_escape(fresh->comm, esc_comm, sizeof esc_comm);
                printf("{\"type\":\"exec\",\"pid\":%" PRIu32
                       ",\"ppid\":%" PRIu32 ",\"uid\":%" PRIu32
                       ",\"comm\":\"%s\",\"argv\":\"%s\""
                       ",\"ts_realtime\":%.6f}\n",
                       pid, fresh->ppid, fresh->uid, esc_comm, esc,
                       realtime_s());
            }
            if (old->ppid != 0 && old->ppid != fresh->ppid) {
                printf("{\"type\":\"reparent\",\"pid\":%" PRIu32
                       ",\"old_ppid\":%" PRIu32 ",\"new_ppid\":%" PRIu32
                       ",\"ts_realtime\":%.6f}\n",
                       pid, old->ppid, fresh->ppid, realtime_s());
            }
            if (old->euid != fresh->euid) {
                printf("{\"type\":\"euid_change\",\"pid\":%" PRIu32
                       ",\"old_euid\":%" PRIu32 ",\"new_euid\":%" PRIu32
                       ",\"ts_realtime\":%.6f}\n",
                       pid, old->euid, fresh->euid, realtime_s());
            }
        }
        if (rescanning) {
            scope_changed = 0;
            printf("{\"type\":\"snapshot_end\",\"ts\":%.6f}\n",
                   realtime_s());
        }

        /* Phase C (exits): watched pids not parsed this poll (gone from
         * the listing, or unreadable) for EXIT_POLLS consecutive polls.
         * O(watched) via the compact list; swap-remove keeps it dense. */
        for (size_t w = 0; w < nwatched; ) {
            uint32_t pid = watched_list[w];
            struct pinfo *pi = &tab[pid];
            if (!pi->alive) {
                watched_list[w] = watched_list[--nwatched];
                continue;
            }
            if (pi->seen_poll != poll_index) pi->unseen++;
            if (pi->unseen >= EXIT_POLLS) {
                pi->alive = 0;
                pi->watched = 0;
                json_escape(pi->comm, esc_comm, sizeof esc_comm);
                printf("{\"type\":\"exit\",\"pid\":%" PRIu32
                       ",\"comm\":\"%s\",\"ts_realtime\":%.6f}\n",
                       pid, esc_comm, realtime_s());
                watched_list[w] = watched_list[--nwatched];
                continue;
            }
            w++;
        }

    sleep:
        polls_since_beat++;
        double work = now_s() - t0;
        double since_beat = now_s() - last_beat;
        if (since_beat >= 1.0) {
            printf("{\"type\":\"heartbeat\",\"polls\":%" PRIu64
                   ",\"listed\":%zd,\"roots\":%zu"
                   ",\"poll_ms\":%.3f,\"interval_ms\":%.1f}\n",
                   polls_since_beat, n, nroots, work * 1000.0,
                   since_beat * 1000.0 / (double)(polls_since_beat
                   ? polls_since_beat : 1));
            last_beat = now_s();
            polls_since_beat = 0;
        }
        if (fflush(stdout) == EOF) {
            /* Broken stdout pipe (consumer died): stop cleanly instead
             * of spinning and burning CPU generating output nobody reads. */
            break;
        }

        double sleep_s = budget - work;
        if (sleep_s < 0.001) sleep_s = 0.001;
        struct timespec ts = {(time_t)sleep_s,
                              (long)((sleep_s - (time_t)sleep_s) * 1e9)};
        nanosleep(&ts, NULL);
    }

    printf("{\"type\":\"snapshot_end\",\"ts\":%.6f}\n", realtime_s());
    fflush(stdout);
    /* Clean shutdown for leak-sensitive builds (ASAN): the pid table,
     * roots, watched list, candidates and the dirent buffer are all
     * process-lifetime allocations. */
    free(tab);
    free(roots);
    free(watched_list);
    free(cand);
    free(listed);
    return 0;
}
