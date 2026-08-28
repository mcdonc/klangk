/* SPDX-License-Identifier: GPL-2.0 */
/* Shared definitions between the eBPF program (procleddy_ebpf.bpf.c) and
 * the userspace loader (procleddy-ebpf.c). Keep in sync with the wire
 * contract documented in scripts/procleddy/procleddy.c. */
#ifndef PROCLEDDY_EBPF_H
#define PROCLEDDY_EBPF_H

#include <linux/types.h>

#define MAX_ROOTS 4096
#define MAX_PARENTS (512 * 1024)
#define WALK_DEPTH 16
#define ARGC_MAX 32
#define ARG_MAX 128
#define ARGV_BUF 2048

/* Event pushed to userspace for every exec of interest. */
struct exec_event {
    __u32 pid;        /* tgid of the exec'ing task (post-exec tid==tgid) */
    __u32 ppid;       /* fork parent tid at first hop, 0 when unknown */
    __u32 uid;
    __u32 euid;
    __u64 ts_ns;      /* CLOCK_MONOTONIC_NS at event time */
    __u8 member;      /* 1: walk reached a root; 0: unknown ancestry —
                         the loader finishes the walk via /proc */
    __u8 argc;
    char comm[16];    /* pre-exec comm (the launcher, e.g. bash) */
    char argv[ARGV_BUF]; /* NUL-separated, argc strings */
};

#endif /* PROCLEDDY_EBPF_H */
