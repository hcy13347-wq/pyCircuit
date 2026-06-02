#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace pyc::actor_v0 {

struct PayloadWord {
  uint32_t port = 0;
  uint64_t value = 0;
};

struct Transaction {
  std::vector<PayloadWord> payload;
};

struct ReadyPattern {
  enum class Kind {
    Constant,
    PeriodicDrive,
  };

  Kind kind = Kind::Constant;
  uint64_t constant_value = 1;
  uint64_t active_value = 0;
  uint64_t default_value = 1;
  uint64_t start_cycle = 0;
  uint64_t end_cycle = 0;
  uint64_t period = 1;
  uint64_t active_cycles = 0;
  uint64_t phase_cycle = 0;

  uint64_t valueAt(uint64_t cycle, uint64_t actor_start, uint64_t actor_end) const {
    if (kind == Kind::Constant) {
      return constant_value;
    }
    const uint64_t start = start_cycle ? start_cycle : actor_start;
    const uint64_t end = end_cycle ? end_cycle : actor_end;
    if (cycle < start || cycle > end) {
      return default_value;
    }
    const uint64_t safe_period = period ? period : 1;
    const uint64_t slot = (cycle - start + phase_cycle) % safe_period;
    return slot < active_cycles ? active_value : default_value;
  }
};

struct PortIo {
  std::function<uint64_t(uint32_t)> read;
  std::function<void(uint32_t, uint64_t)> write;
};

struct OrderedScoreboardResult {
  std::string name;
  uint64_t expected_count = 0;
  uint64_t actual_count = 0;
  uint64_t mismatch_count = 0;
  std::vector<std::string> messages;
};

class OrderedScoreboard {
 public:
  OrderedScoreboard(std::string name, std::vector<uint32_t> payload_ports, std::vector<Transaction> expected)
      : name_(std::move(name)), payload_ports_(std::move(payload_ports)), expected_(std::move(expected)) {}

  void sample(uint64_t cycle, const std::vector<PayloadWord>& actual_payload) {
    const uint64_t index = actual_count_;
    ++actual_count_;
    if (index >= expected_.size()) {
      ++mismatch_count_;
      messages_.push_back("extra actual transaction at cycle " + std::to_string(cycle));
      return;
    }

    for (const auto port : payload_ports_) {
      const uint64_t actual = lookup(actual_payload, port);
      const uint64_t expected = lookup(expected_[index].payload, port);
      if (actual != expected) {
        ++mismatch_count_;
        messages_.push_back(
            "mismatch index=" + std::to_string(index) +
            " port=" + std::to_string(port) +
            " cycle=" + std::to_string(cycle) +
            " expected=0x" + hex(expected) +
            " actual=0x" + hex(actual));
      }
    }
  }

  OrderedScoreboardResult finish() const {
    OrderedScoreboardResult result;
    result.name = name_;
    result.expected_count = expected_.size();
    result.actual_count = actual_count_;
    result.mismatch_count = mismatch_count_;
    result.messages = messages_;
    if (actual_count_ < expected_.size()) {
      result.mismatch_count += expected_.size() - actual_count_;
      result.messages.push_back("missing " + std::to_string(expected_.size() - actual_count_) + " transaction(s)");
    }
    return result;
  }

 private:
  static uint64_t lookup(const std::vector<PayloadWord>& payload, uint32_t port) {
    for (const auto& word : payload) {
      if (word.port == port) {
        return word.value;
      }
    }
    return 0;
  }

  static std::string hex(uint64_t value) {
    static constexpr char kDigits[] = "0123456789abcdef";
    if (value == 0) {
      return "0";
    }
    std::string out;
    while (value) {
      out.push_back(kDigits[value & 0xf]);
      value >>= 4;
    }
    std::reverse(out.begin(), out.end());
    return out;
  }

  std::string name_;
  std::vector<uint32_t> payload_ports_;
  std::vector<Transaction> expected_;
  uint64_t actual_count_ = 0;
  uint64_t mismatch_count_ = 0;
  std::vector<std::string> messages_;
};

class ReadyValidSource {
 public:
  ReadyValidSource(
      std::string name,
      uint32_t valid_port,
      uint32_t ready_port,
      std::vector<Transaction> transactions,
      uint64_t start_cycle,
      uint64_t end_cycle)
      : name_(std::move(name)),
        valid_port_(valid_port),
        ready_port_(ready_port),
        transactions_(std::move(transactions)),
        start_cycle_(start_cycle),
        end_cycle_(end_cycle) {}

  void preCycle(uint64_t cycle, const PortIo& io) {
    if (cycle < start_cycle_ || cycle > end_cycle_ || tx_index_ >= transactions_.size()) {
      io.write(valid_port_, 0);
      return;
    }
    io.write(valid_port_, 1);
    for (const auto& word : transactions_[tx_index_].payload) {
      io.write(word.port, word.value);
    }
    ++drive_cycles_;
  }

  void postCycle(uint64_t cycle, const PortIo& io) {
    if (cycle < start_cycle_ || cycle > end_cycle_ || tx_index_ >= transactions_.size()) {
      return;
    }
    if (io.read(ready_port_) != 0) {
      ++tx_index_;
    }
  }

  uint64_t completed() const { return tx_index_; }
  uint64_t total() const { return transactions_.size(); }
  uint64_t driveCycles() const { return drive_cycles_; }
  const std::string& name() const { return name_; }

 private:
  std::string name_;
  uint32_t valid_port_ = 0;
  uint32_t ready_port_ = 0;
  std::vector<Transaction> transactions_;
  uint64_t start_cycle_ = 0;
  uint64_t end_cycle_ = 0;
  uint64_t tx_index_ = 0;
  uint64_t drive_cycles_ = 0;
};

class ReadyValidSink {
 public:
  ReadyValidSink(
      std::string name,
      uint32_t valid_port,
      uint32_t ready_port,
      std::vector<uint32_t> payload_ports,
      ReadyPattern ready_pattern,
      uint64_t start_cycle,
      uint64_t end_cycle,
      OrderedScoreboard* scoreboard)
      : name_(std::move(name)),
        valid_port_(valid_port),
        ready_port_(ready_port),
        payload_ports_(std::move(payload_ports)),
        ready_pattern_(ready_pattern),
        start_cycle_(start_cycle),
        end_cycle_(end_cycle),
        scoreboard_(scoreboard) {}

  void preCycle(uint64_t cycle, const PortIo& io) {
    if (cycle < start_cycle_ || cycle > end_cycle_) {
      io.write(ready_port_, 0);
      return;
    }
    io.write(ready_port_, ready_pattern_.valueAt(cycle, start_cycle_, end_cycle_));
  }

  void postCycle(uint64_t cycle, const PortIo& io) {
    if (cycle < start_cycle_ || cycle > end_cycle_) {
      return;
    }
    const uint64_t valid = io.read(valid_port_);
    const uint64_t ready = io.read(ready_port_);
    if (!valid || !ready) {
      return;
    }

    std::vector<PayloadWord> payload;
    payload.reserve(payload_ports_.size());
    for (const auto port : payload_ports_) {
      payload.push_back(PayloadWord{port, io.read(port)});
    }
    ++sample_count_;
    if (scoreboard_ != nullptr) {
      scoreboard_->sample(cycle, payload);
    }
  }

  uint64_t sampleCount() const { return sample_count_; }
  const std::string& name() const { return name_; }

 private:
  std::string name_;
  uint32_t valid_port_ = 0;
  uint32_t ready_port_ = 0;
  std::vector<uint32_t> payload_ports_;
  ReadyPattern ready_pattern_;
  uint64_t start_cycle_ = 0;
  uint64_t end_cycle_ = 0;
  OrderedScoreboard* scoreboard_ = nullptr;
  uint64_t sample_count_ = 0;
};

struct RuntimeResult {
  uint64_t cycles = 0;
  std::vector<OrderedScoreboardResult> scoreboards;
};

class ReadyValidActorRuntime {
 public:
  explicit ReadyValidActorRuntime(PortIo io) : io_(std::move(io)) {
    if (!io_.read || !io_.write) {
      throw std::invalid_argument("PortIo read/write callbacks must be set");
    }
  }

  void addSource(ReadyValidSource source) { sources_.push_back(std::move(source)); }
  void addSink(ReadyValidSink sink) { sinks_.push_back(std::move(sink)); }
  void addScoreboard(OrderedScoreboard scoreboard) { scoreboards_.push_back(std::move(scoreboard)); }

  OrderedScoreboard* scoreboard(size_t index) {
    if (index >= scoreboards_.size()) {
      return nullptr;
    }
    return &scoreboards_[index];
  }

  template <typename EvalFn>
  RuntimeResult run(uint64_t start_cycle, uint64_t end_cycle, EvalFn&& eval) {
    for (uint64_t cycle = start_cycle; cycle <= end_cycle; ++cycle) {
      for (auto& source : sources_) {
        source.preCycle(cycle, io_);
      }
      for (auto& sink : sinks_) {
        sink.preCycle(cycle, io_);
      }
      eval(cycle);
      for (auto& source : sources_) {
        source.postCycle(cycle, io_);
      }
      for (auto& sink : sinks_) {
        sink.postCycle(cycle, io_);
      }
    }

    RuntimeResult result;
    result.cycles = end_cycle >= start_cycle ? end_cycle - start_cycle + 1 : 0;
    for (const auto& scoreboard : scoreboards_) {
      result.scoreboards.push_back(scoreboard.finish());
    }
    return result;
  }

 private:
  PortIo io_;
  std::vector<ReadyValidSource> sources_;
  std::vector<ReadyValidSink> sinks_;
  std::vector<OrderedScoreboard> scoreboards_;
};

}  // namespace pyc::actor_v0
