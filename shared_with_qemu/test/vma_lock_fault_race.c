#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef PAGE_SIZE
#define PAGE_SIZE 4096UL
#endif

struct config {
	const char *path;
	size_t file_size;
	int readers;
	int writer_delay_ms;
	int writer_max_iters;
	int reader_rounds;
	int reader_pause_us;
	bool drop_caches;
	bool pin_cpus;
};

struct shared_ctx {
	struct config cfg;
	int fd;
	uint8_t *map;
	size_t nr_pages;
	pthread_barrier_t start_barrier;
	atomic_int stop_writer;
	atomic_ulong total_fault_reads;
};

struct thread_arg {
	struct shared_ctx *ctx;
	int tid;
};

static void die_errno(const char *msg)
{
	fprintf(stderr, "%s: %s\n", msg, strerror(errno));
	exit(1);
}

static void write_full(int fd, const void *buf, size_t len, off_t off)
{
	const uint8_t *p = buf;

	while (len) {
		ssize_t ret = pwrite(fd, p, len, off);

		if (ret < 0)
			die_errno("pwrite");
		p += ret;
		off += ret;
		len -= (size_t)ret;
	}
}

static void prepare_backing_file(const struct config *cfg, int fd)
{
	size_t offset = 0;
	size_t chunk_len = 1U << 20;
	uint8_t *chunk = malloc(chunk_len);

	if (!chunk) {
		fprintf(stderr, "malloc failed\n");
		exit(1);
	}

	for (size_t i = 0; i < chunk_len; i++)
		chunk[i] = (uint8_t)(i * 131U + 17U);

	if (ftruncate(fd, (off_t)cfg->file_size))
		die_errno("ftruncate");

	while (offset < cfg->file_size) {
		size_t this_len = cfg->file_size - offset;

		if (this_len > chunk_len)
			this_len = chunk_len;
		write_full(fd, chunk, this_len, (off_t)offset);
		offset += this_len;
	}

	if (fsync(fd))
		die_errno("fsync");
	free(chunk);
}

static void best_effort_drop_cache(const struct config *cfg, int fd)
{
	if (fdatasync(fd))
		die_errno("fdatasync");
	if (posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED))
		fprintf(stderr, "posix_fadvise(DONTNEED) failed\n");
	if (!cfg->drop_caches)
		return;
	if (syncfs(fd))
		die_errno("syncfs");
	if (system("sync") != 0)
		fprintf(stderr, "sync command failed\n");
	if (system("echo 3 > /proc/sys/vm/drop_caches") != 0)
		fprintf(stderr, "drop_caches failed; continue with page-cache state as-is\n");
}

static void maybe_pin_cpu(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	if (sched_setaffinity(0, sizeof(set), &set))
		fprintf(stderr, "sched_setaffinity(cpu=%d) failed: %s\n",
			cpu, strerror(errno));
}

static void *reader_thread(void *arg)
{
	struct thread_arg *targ = arg;
	struct shared_ctx *ctx = targ->ctx;
	const struct config *cfg = &ctx->cfg;
	volatile uint8_t sink = 0;

	if (cfg->pin_cpus)
		maybe_pin_cpu(targ->tid % sysconf(_SC_NPROCESSORS_ONLN));

	pthread_barrier_wait(&ctx->start_barrier);
	for (int round = 0; round < cfg->reader_rounds; round++) {
		for (size_t page = (size_t)targ->tid; page < ctx->nr_pages;
		     page += (size_t)cfg->readers) {
			size_t offset = page * PAGE_SIZE;

			sink ^= ctx->map[offset];
			atomic_fetch_add_explicit(&ctx->total_fault_reads, 1,
						  memory_order_relaxed);
			if (cfg->reader_pause_us)
				usleep((useconds_t)cfg->reader_pause_us);
		}
	}

	if (sink == 0xFF)
		fprintf(stderr, "reader sink=%u\n", sink);
	return NULL;
}

static void *writer_thread(void *arg)
{
	struct thread_arg *targ = arg;
	struct shared_ctx *ctx = targ->ctx;
	const struct config *cfg = &ctx->cfg;
	int iters = 0;

	if (cfg->pin_cpus)
		maybe_pin_cpu((cfg->readers + targ->tid) % sysconf(_SC_NPROCESSORS_ONLN));

	pthread_barrier_wait(&ctx->start_barrier);
	if (cfg->writer_delay_ms > 0)
		usleep((useconds_t)cfg->writer_delay_ms * 1000U);

	while (!atomic_load_explicit(&ctx->stop_writer, memory_order_relaxed) &&
	       iters < cfg->writer_max_iters) {
		if (mprotect(ctx->map, ctx->cfg.file_size, PROT_READ))
			die_errno("mprotect(PROT_READ)");
		if (mprotect(ctx->map, ctx->cfg.file_size, PROT_READ | PROT_WRITE))
			die_errno("mprotect(PROT_READ|PROT_WRITE)");
		iters++;
		if ((iters & 0x3f) == 0)
			sched_yield();
	}

	fprintf(stderr, "writer iters=%d\n", iters);
	return NULL;
}

static long parse_long(const char *s, const char *name)
{
	char *end = NULL;
	long v = strtol(s, &end, 10);

	if (!s[0] || (end && *end)) {
		fprintf(stderr, "invalid %s: %s\n", name, s);
		exit(1);
	}
	return v;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s <path> [--file-mb N] [--readers N] [--writer-delay-ms N]\n"
		"          [--writer-iters N] [--reader-rounds N] [--reader-pause-us N]\n"
		"          [--drop-caches] [--pin-cpus]\n",
		prog);
	exit(2);
}

int main(int argc, char **argv)
{
	struct shared_ctx ctx = {
		.cfg = {
			.file_size = 256UL << 20,
			.readers = 4,
			.writer_delay_ms = 10,
			.writer_max_iters = 200000,
			.reader_rounds = 1,
			.reader_pause_us = 0,
			.drop_caches = false,
			.pin_cpus = false,
		},
	};
	pthread_t *reader_threads = NULL;
	struct thread_arg *reader_args = NULL;
	pthread_t writer;
	struct thread_arg writer_arg = {
		.ctx = &ctx,
		.tid = 0,
	};

	if (argc < 2)
		usage(argv[0]);
	ctx.cfg.path = argv[1];
	for (int i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--file-mb") && i + 1 < argc) {
			ctx.cfg.file_size = (size_t)parse_long(argv[++i], "file-mb") << 20;
		} else if (!strcmp(argv[i], "--readers") && i + 1 < argc) {
			ctx.cfg.readers = (int)parse_long(argv[++i], "readers");
		} else if (!strcmp(argv[i], "--writer-delay-ms") && i + 1 < argc) {
			ctx.cfg.writer_delay_ms = (int)parse_long(argv[++i], "writer-delay-ms");
		} else if (!strcmp(argv[i], "--writer-iters") && i + 1 < argc) {
			ctx.cfg.writer_max_iters = (int)parse_long(argv[++i], "writer-iters");
		} else if (!strcmp(argv[i], "--reader-rounds") && i + 1 < argc) {
			ctx.cfg.reader_rounds = (int)parse_long(argv[++i], "reader-rounds");
		} else if (!strcmp(argv[i], "--reader-pause-us") && i + 1 < argc) {
			ctx.cfg.reader_pause_us = (int)parse_long(argv[++i], "reader-pause-us");
		} else if (!strcmp(argv[i], "--drop-caches")) {
			ctx.cfg.drop_caches = true;
		} else if (!strcmp(argv[i], "--pin-cpus")) {
			ctx.cfg.pin_cpus = true;
		} else {
			usage(argv[0]);
		}
	}

	if (ctx.cfg.readers <= 0 || ctx.cfg.file_size < PAGE_SIZE)
		usage(argv[0]);

	ctx.fd = open(ctx.cfg.path, O_CREAT | O_RDWR | O_TRUNC | O_CLOEXEC, 0644);
	if (ctx.fd < 0)
		die_errno("open");

	prepare_backing_file(&ctx.cfg, ctx.fd);
	best_effort_drop_cache(&ctx.cfg, ctx.fd);

	ctx.map = mmap(NULL, ctx.cfg.file_size, PROT_READ | PROT_WRITE,
		       MAP_SHARED, ctx.fd, 0);
	if (ctx.map == MAP_FAILED)
		die_errno("mmap");
	ctx.nr_pages = ctx.cfg.file_size / PAGE_SIZE;

	if (pthread_barrier_init(&ctx.start_barrier, NULL,
				 (unsigned int)ctx.cfg.readers + 1))
		die_errno("pthread_barrier_init");

	reader_threads = calloc((size_t)ctx.cfg.readers, sizeof(*reader_threads));
	reader_args = calloc((size_t)ctx.cfg.readers, sizeof(*reader_args));
	if (!reader_threads || !reader_args) {
		fprintf(stderr, "calloc failed\n");
		exit(1);
	}

	for (int i = 0; i < ctx.cfg.readers; i++) {
		reader_args[i].ctx = &ctx;
		reader_args[i].tid = i;
		if (pthread_create(&reader_threads[i], NULL, reader_thread,
				   &reader_args[i]))
			die_errno("pthread_create(reader)");
	}
	if (pthread_create(&writer, NULL, writer_thread, &writer_arg))
		die_errno("pthread_create(writer)");

	for (int i = 0; i < ctx.cfg.readers; i++) {
		if (pthread_join(reader_threads[i], NULL))
			die_errno("pthread_join(reader)");
	}
	atomic_store_explicit(&ctx.stop_writer, 1, memory_order_relaxed);
	if (pthread_join(writer, NULL))
		die_errno("pthread_join(writer)");

	fprintf(stderr, "fault_reads=%lu\n",
		atomic_load_explicit(&ctx.total_fault_reads, memory_order_relaxed));

	munmap(ctx.map, ctx.cfg.file_size);
	close(ctx.fd);
	free(reader_threads);
	free(reader_args);
	return 0;
}
