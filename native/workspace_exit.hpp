#pragma once
#include <string>
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <sys/wait.h>
#include <unistd.h>
namespace fw {
inline bool spawnLocalExit(const std::string &helper, const std::string &requestDir,
                           const std::string &session, const std::string &operation) {
  if (helper.empty() || access(helper.c_str(), X_OK) != 0 ||
      (operation != "stop" && operation != "exit" && operation != "exit-current")) return false;
  int execPipe[2];
  if (pipe(execPipe) != 0) return false;
  if (fcntl(execPipe[1], F_SETFD, FD_CLOEXEC) != 0) {
    close(execPipe[0]); close(execPipe[1]); return false;
  }
  const pid_t child = fork();
  if (child < 0) { close(execPipe[0]); close(execPipe[1]); return false; }
  if (child == 0) {
    close(execPipe[0]);
    const pid_t worker = fork();
    if (worker < 0) {
      const int error = errno; write(execPipe[1], &error, sizeof(error)); _exit(126);
    }
    if (worker > 0) _exit(0);
    if (setsid() < 0) {
      const int error = errno; write(execPipe[1], &error, sizeof(error)); _exit(126);
    }
    if (operation == "exit-current")
      execl(helper.c_str(), helper.c_str(), "exit-current", "--request-dir", requestDir.c_str(),
            static_cast<char *>(nullptr));
    else if (operation == "exit")
      execl(helper.c_str(), helper.c_str(), "exit", "--session", session.c_str(),
            "--request-dir", requestDir.c_str(), static_cast<char *>(nullptr));
    else
      execl(helper.c_str(), helper.c_str(), "stop", "--session", session.c_str(),
            static_cast<char *>(nullptr));
    const int error = errno; write(execPipe[1], &error, sizeof(error));
    _exit(127);
  }
  close(execPipe[1]);
  int status = 0;
  while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
  struct pollfd event {execPipe[0], POLLIN | POLLHUP, 0};
  int ready;
  do { ready = poll(&event, 1, 1000); } while (ready < 0 && errno == EINTR);
  int childError = 0;
  ssize_t bytes = -1;
  if (ready > 0) {
    do { bytes = read(execPipe[0], &childError, sizeof(childError)); }
    while (bytes < 0 && errno == EINTR);
  }
  close(execPipe[0]);
  return WIFEXITED(status) && WEXITSTATUS(status) == 0 && ready > 0 && bytes == 0;
}

} // namespace fw
