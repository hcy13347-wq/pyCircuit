#pragma once

#include <cstdint>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace pyc::cpp {

template <std::size_t MaxWords>
struct RuntimeLoopEvent {
  std::uint64_t cycle = 0;
  std::uint32_t port_id = 0;
  std::uint32_t nwords = 0;
  std::array<std::uint64_t, MaxWords> words{};
  std::string msg;
};

template <std::size_t MaxWords, std::size_t MaxDrivePorts>
struct RuntimeLoopDriveFrame {
  static constexpr std::size_t kMaskWords = (MaxDrivePorts + 63u) / 64u;

  std::uint64_t cycle = 0;
  std::array<std::uint64_t, kMaskWords> port_mask{};
  std::array<std::array<std::uint64_t, MaxWords>, MaxDrivePorts> words{};
};

template <std::size_t MaxWords, std::size_t MaxDrivePorts = 0>
struct RuntimeLoopSchedule {
  std::vector<RuntimeLoopDriveFrame<MaxWords, MaxDrivePorts>> drive_frames;
  std::vector<RuntimeLoopEvent<MaxWords>> pre_expect_events;
  std::vector<RuntimeLoopEvent<MaxWords>> post_expect_events;
};

template <std::size_t MaxWords>
struct RuntimeLoopPeriodicDrive {
  std::uint32_t port_id = 0;
  std::uint64_t start_cycle = 0;
  std::uint64_t end_cycle = 0;
  std::uint64_t period = 1;
  std::uint64_t active_cycles = 0;
  std::uint64_t phase_cycle = 0;
  std::uint32_t nwords = 0;
  std::array<std::uint64_t, MaxWords> active_words{};
  std::array<std::uint64_t, MaxWords> default_words{};

  bool activeAt(std::uint64_t cycle) const {
    if (cycle < start_cycle || cycle >= end_cycle || period == 0) return false;
    return ((cycle - phase_cycle) % period) < active_cycles;
  }
};

template <std::size_t MaxWords, std::size_t MaxDrivePorts = 0>
struct RuntimeLoopPatternSchedule {
  std::vector<RuntimeLoopPeriodicDrive<MaxWords>> periodic_drives;
};

namespace detail {

template <typename T>
inline bool readPod(std::ifstream &in, T &out) {
  in.read(reinterpret_cast<char *>(&out), sizeof(T));
  return static_cast<bool>(in);
}

template <std::size_t MaxWords>
inline bool readRuntimeLoopEvent(std::ifstream &in, RuntimeLoopEvent<MaxWords> &ev) {
  std::uint32_t msgLen = 0;
  if (!readPod(in, ev.cycle) || !readPod(in, ev.port_id) || !readPod(in, ev.nwords) || !readPod(in, msgLen))
    return false;
  if (ev.nwords > MaxWords)
    return false;
  ev.words.fill(0);
  for (std::size_t i = 0; i < MaxWords; ++i) {
    if (!readPod(in, ev.words[i]))
      return false;
  }
  if (msgLen > (1u << 20))
    return false;
  ev.msg.resize(msgLen);
  if (msgLen != 0) {
    in.read(ev.msg.data(), static_cast<std::streamsize>(msgLen));
    if (!in)
      return false;
  }
  return true;
}

template <std::size_t MaxWords, std::size_t MaxDrivePorts>
inline bool readRuntimeLoopDriveFrame(std::ifstream &in, RuntimeLoopDriveFrame<MaxWords, MaxDrivePorts> &frame) {
  if (!readPod(in, frame.cycle))
    return false;
  for (std::size_t i = 0; i < frame.port_mask.size(); ++i) {
    if (!readPod(in, frame.port_mask[i]))
      return false;
  }
  for (std::size_t port = 0; port < MaxDrivePorts; ++port) {
    for (std::size_t word = 0; word < MaxWords; ++word) {
      if (!readPod(in, frame.words[port][word]))
        return false;
    }
  }
  return true;
}

template <std::size_t MaxWords, std::size_t MaxDrivePorts>
inline bool readRuntimeLoopDriveFrames(std::ifstream &in,
                                       std::uint64_t count,
                                       std::vector<RuntimeLoopDriveFrame<MaxWords, MaxDrivePorts>> &frames) {
  frames.clear();
  frames.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    RuntimeLoopDriveFrame<MaxWords, MaxDrivePorts> frame;
    if (!readRuntimeLoopDriveFrame(in, frame))
      return false;
    frames.push_back(std::move(frame));
  }
  return true;
}

template <std::size_t MaxWords>
inline bool readRuntimeLoopSection(std::ifstream &in,
                                   std::uint64_t count,
                                   std::vector<RuntimeLoopEvent<MaxWords>> &events) {
  events.clear();
  events.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    RuntimeLoopEvent<MaxWords> ev;
    if (!readRuntimeLoopEvent(in, ev))
      return false;
    events.push_back(std::move(ev));
  }
  return true;
}

} // namespace detail

template <std::size_t MaxWords>
inline bool loadRuntimeLoopSchedule(const std::filesystem::path &path,
                                    RuntimeLoopSchedule<MaxWords> &schedule) {
  std::ifstream in(path, std::ios::binary);
  if (!in.is_open()) {
    std::cerr << "ERROR: failed to open runtime-loop schedule: " << path << "\n";
    return false;
  }

  std::string magic(8, '\0');
  in.read(magic.data(), static_cast<std::streamsize>(magic.size()));
  if (magic != std::string("PYCSTB2\n", 8)) {
    std::cerr << "ERROR: invalid runtime-loop schedule magic: " << path << "\n";
    return false;
  }

  std::uint32_t maxWords = 0;
  std::uint64_t driveCount = 0;
  std::uint64_t preExpectCount = 0;
  std::uint64_t postExpectCount = 0;
  if (!detail::readPod(in, maxWords) || !detail::readPod(in, driveCount) || !detail::readPod(in, preExpectCount)
      || !detail::readPod(in, postExpectCount)) {
    std::cerr << "ERROR: truncated runtime-loop schedule header: " << path << "\n";
    return false;
  }
  if (maxWords != MaxWords) {
    std::cerr << "ERROR: runtime-loop schedule word width mismatch: " << path << "\n";
    return false;
  }

  if (!detail::readRuntimeLoopSection(in, driveCount, schedule.drive_events)
      || !detail::readRuntimeLoopSection(in, preExpectCount, schedule.pre_expect_events)
      || !detail::readRuntimeLoopSection(in, postExpectCount, schedule.post_expect_events)) {
    std::cerr << "ERROR: truncated runtime-loop schedule payload: " << path << "\n";
    return false;
  }
  return true;
}

template <std::size_t MaxWords, std::size_t MaxDrivePorts>
inline bool loadRuntimeLoopSchedule(const std::filesystem::path &path,
                                    RuntimeLoopSchedule<MaxWords, MaxDrivePorts> &schedule) {
  std::ifstream in(path, std::ios::binary);
  if (!in.is_open()) {
    std::cerr << "ERROR: failed to open runtime-loop schedule: " << path << "\n";
    return false;
  }

  std::string magic(8, '\0');
  in.read(magic.data(), static_cast<std::streamsize>(magic.size()));
  if (magic != std::string("PYCSTB3\n", 8)) {
    std::cerr << "ERROR: invalid runtime-loop schedule magic: " << path << "\n";
    return false;
  }

  std::uint32_t maxWords = 0;
  std::uint32_t drivePorts = 0;
  std::uint64_t driveFrameCount = 0;
  std::uint64_t preExpectCount = 0;
  std::uint64_t postExpectCount = 0;
  if (!detail::readPod(in, maxWords) || !detail::readPod(in, drivePorts)
      || !detail::readPod(in, driveFrameCount) || !detail::readPod(in, preExpectCount)
      || !detail::readPod(in, postExpectCount)) {
    std::cerr << "ERROR: truncated runtime-loop schedule header: " << path << "\n";
    return false;
  }
  if (maxWords != MaxWords || drivePorts != MaxDrivePorts) {
    std::cerr << "ERROR: runtime-loop schedule shape mismatch: " << path << "\n";
    return false;
  }

  if (!detail::readRuntimeLoopDriveFrames(in, driveFrameCount, schedule.drive_frames)
      || !detail::readRuntimeLoopSection(in, preExpectCount, schedule.pre_expect_events)
      || !detail::readRuntimeLoopSection(in, postExpectCount, schedule.post_expect_events)) {
    std::cerr << "ERROR: truncated runtime-loop schedule payload: " << path << "\n";
    return false;
  }
  return true;
}

} // namespace pyc::cpp
