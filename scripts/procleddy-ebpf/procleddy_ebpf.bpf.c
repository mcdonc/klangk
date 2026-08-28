// SPDX-License-Identifier: GPL-2.0
/* procleddy-ebpf BPF side (#2520 spike): real-time process-launch events.
 *
 * Hook points (all stable tracepoints; no kernel structs are read, so no
 * vmlinux.h / CO-RE is needed):
 *
 *   sched/sched_process_fork  -> parents[child_tid] = parent_tid
 *   sched/sched_process_exit  -> parents.delete(tid)
 *   syscalls/sys_enter_execve[_at] -> capture argv + uid, walk the parents
 *                                     map toward the configured roots, and
 *                                     push one event per exec of interest
 *                                     to the ring buffer.
 *
 * The userspace loader (procleddy-ebpf.c) owns the roots map (updated from
 * stdin scope lines, same contract as the /proc poller) and turns ring
 * buffer events into the NDJSON stream klangkd already consumes, so the
 * eBPF backend is a drop-in replacement for procleddy.
 *
 * Spike limitations (documented in docs/features/process-ledger.md):
 *   - processes forked before the monitor started have no parents-map
 *     entry; such events are flagged unknown and the loader finishes the
 *     walk from /proc as a fallback.
 *   - argv is truncated (32 args, 128 bytes each, 2 KiB total).
 *   - sid is not captured (the ledger does not consume it).
 *   - thread forks are tracked in the map; they are harmless: threads do
 *     not emit events unless they exec, and exec makes the exec'ing task
 *     the group leader, so its tid == tgid.
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#include "procleddy_ebpf.h"

/* Tracepoint argument layouts, mirrored from the format files under
 * /sys/kernel/tracing/events (stable UAPI). */
struct tp_fork {
    char parent_comm[16];
    int parent_pid;
    char child_comm[16];
    int child_pid;
};

struct tp_exit {
    char comm[16];
    int pid;
    int prio;
};

struct tp_sys_enter {
    unsigned short common_type;
    unsigned char common_flags;
    unsigned char common_preempt_count;
    int common_pid;
    long syscall_nr;
    unsigned long args[6];
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_PARENTS);
    __type(key, __u32);     /* child tid */
    __type(value, __u32);   /* parent tid */
} parents SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_ROOTS);
    __type(key, __u32);     /* container-init host pid (tgid) */
    __type(value, __u8);
} roots SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 20);
} events SEC(".maps");

SEC("tracepoint/sched/sched_process_fork")
int on_fork(struct tp_fork *ctx)
{
    __u32 child = (__u32)ctx->child_pid, parent = (__u32)ctx->parent_pid;
    if (child && child != parent)
        bpf_map_update_elem(&parents, &child, &parent, BPF_ANY);
    return 0;
}

SEC("tracepoint/sched/sched_process_exit")
int on_exit(struct tp_exit *ctx)
{
    __u32 pid = (__u32)ctx->pid;
    if (pid)
        bpf_map_delete_elem(&parents, &pid);
    return 0;
}

static __always_inline void capture_exec(const char *filename,
                                         const char *const *argv)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 tid = (__u32)pid_tgid;
    __u32 tgid = pid_tgid >> 32;
    __u64 ug = bpf_get_current_uid_gid();

    /* Walk the fork map toward the roots — the eBPF equivalent of the
     * /proc poller's ancestry walk, with ppid-at-launch semantics. A
     * completed walk that never reached a root is a conclusive
     * non-member (host noise): dropped here, never forwarded. A map miss
     * means "forked before the monitor started": forwarded with the
     * unknown flag so the loader can finish the walk from /proc. */
    __u32 cur = tid, ppid = 0;
    __u8 miss = 0, member = 0;
    _Pragma("unroll")
    for (int i = 0; i < WALK_DEPTH; i++) {
        __u32 *pp = bpf_map_lookup_elem(&parents, &cur);
        if (!pp) {
            miss = 1;
            break;
        }
        ppid = *pp;
        if (bpf_map_lookup_elem(&roots, &cur) ||
            bpf_map_lookup_elem(&roots, &ppid)) {
            member = 1;
            break;
        }
        if (*pp == cur || *pp <= 1)
            break; /* walked to init — conclusively outside workspaces */
        cur = *pp;
    }
    if (!member && !miss)
        return;

    struct exec_event *ev = bpf_ringbuf_reserve(&events, sizeof *ev, 0);
    if (!ev)
        return;
    /* No memset: the BPF backend rejects it. Userspace reads only the
     * assigned scalars, the NUL-terminated comm (bpf_get_current_comm
     * always terminates), and argc NUL-terminated argv strings — garbage
     * past those bounds is never serialized. */
    ev->pid = tgid;
    ev->ppid = member ? ppid : 0;
    ev->uid = (__u32)(ug >> 32);
    ev->euid = ev->uid;
    ev->ts_ns = bpf_ktime_get_ns();
    ev->member = member;
    ev->argc = 0;
    bpf_get_current_comm(ev->comm, sizeof ev->comm);

    /* filename becomes argv[0]; then up to ARGC_MAX-1 argv strings,
     * NUL-packed into the fixed tail. */
    long off = 0;
    long n = bpf_probe_read_user_str(&ev->argv[0], ARG_MAX, filename);
    if (n > 0) {
        ev->argc = 1;
        off = n;
    }
    if (argv) {
        _Pragma("unroll")
        for (int i = 0; i < ARGC_MAX - 1; i++) {
            const char *arg = NULL;
            if (off + ARG_MAX > ARGV_BUF)
                break;
            if (bpf_probe_read_user(&arg, sizeof arg, &argv[i]) != 0)
                break;
            if (!arg)
                break;
            n = bpf_probe_read_user_str(&ev->argv[off], ARG_MAX, arg);
            if (n <= 0)
                break;
            off += n;
            ev->argc++;
        }
    }
    bpf_ringbuf_submit(ev, 0);
}

SEC("tracepoint/syscalls/sys_enter_execve")
int on_execve(struct tp_sys_enter *ctx)
{
    capture_exec((const char *)ctx->args[0],
                 (const char *const *)ctx->args[1]);
    return 0;
}

SEC("tracepoint/syscalls/sys_enter_execveat")
int on_execveat(struct tp_sys_enter *ctx)
{
    /* int execveat(int fd, const char *filename, char *const argv[], ...) */
    capture_exec((const char *)ctx->args[1],
                 (const char *const *)ctx->args[2]);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
