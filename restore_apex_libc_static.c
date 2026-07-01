#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static int copy_file(const char *src, const char *dst)
{
	int in = -1, out = -1, rc = 1;
	char buf[65536];
	ssize_t n;

	in = open(src, O_RDONLY | O_CLOEXEC);
	if (in < 0) {
		fprintf(stderr, "open src %s: %s\n", src, strerror(errno));
		goto out;
	}

	out = open(dst, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (out < 0) {
		fprintf(stderr, "open dst %s: %s\n", dst, strerror(errno));
		goto out;
	}

	while ((n = read(in, buf, sizeof(buf))) > 0) {
		char *p = buf;
		ssize_t left = n;

		while (left > 0) {
			ssize_t w = write(out, p, left);
			if (w < 0) {
				fprintf(stderr, "write %s: %s\n", dst, strerror(errno));
				goto out;
			}
			p += w;
			left -= w;
		}
	}
	if (n < 0) {
		fprintf(stderr, "read %s: %s\n", src, strerror(errno));
		goto out;
	}
	if (fsync(out) < 0) {
		fprintf(stderr, "fsync %s: %s\n", dst, strerror(errno));
		goto out;
	}
	rc = 0;

out:
	if (out >= 0)
		close(out);
	if (in >= 0)
		close(in);
	return rc;
}

int main(int argc, char **argv)
{
	const char *src = "/data/local/tmp/libc_original.so";
	const char *dst = "/apex/com.android.runtime/lib64/bionic/libc.so";
	const char *tmp = "/apex/com.android.runtime/lib64/bionic/libc.so.restore_tmp";

	if (argc >= 2)
		src = argv[1];
	if (argc >= 3)
		dst = argv[2];
	if (argc >= 4)
		tmp = argv[3];

	if (copy_file(src, tmp) != 0)
		return 1;

	if (rename(tmp, dst) < 0) {
		fprintf(stderr, "rename %s -> %s: %s\n", tmp, dst, strerror(errno));
		unlink(tmp);
		return 1;
	}

	printf("restored %s from %s\n", dst, src);
	return 0;
}
