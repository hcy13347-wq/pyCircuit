#pragma once

#include <cstdint>
#include <cstddef>
#include <exception>
#include <fstream>
#include <iomanip>
#include <memory>
#include <stdexcept>
#include <string>
#include <sstream>
#include <utility>
#include <vector>

#include <cpp/pyc_tb_actor_v0.hpp>
#include <cpp/pyc_tb_pycstb4.hpp>

namespace pyc::workload_v0 {

struct InstructionWord {
  std::uint64_t index = 0;
  std::uint64_t value = 0;
  std::uint32_t width = 0;
};

struct PolicyDecision {
  bool should_check = true;
  bool stop_on_mismatch = false;
};

class ScoreboardPolicyRuntime {
 public:
  virtual ~ScoreboardPolicyRuntime() = default;
  virtual const pyc::cpp::Pycstb4ScoreboardPolicy& metadata() const = 0;
  virtual PolicyDecision decision(std::uint64_t index) const = 0;
};

class SignatureScoreboardPolicy final : public ScoreboardPolicyRuntime {
 public:
  explicit SignatureScoreboardPolicy(pyc::cpp::Pycstb4ScoreboardPolicy policy)
      : policy_(std::move(policy)) {
    if (policy_.kind != "signature") {
      throw std::runtime_error("SignatureScoreboardPolicy requires kind=signature");
    }
    if (policy_.signature.empty()) {
      throw std::runtime_error("SignatureScoreboardPolicy requires non-empty signature");
    }
  }

  const pyc::cpp::Pycstb4ScoreboardPolicy& metadata() const override { return policy_; }

  PolicyDecision decision(std::uint64_t) const override {
    return PolicyDecision{/*should_check=*/false, /*stop_on_mismatch=*/(policy_.flags & 1ull) != 0ull};
  }

  static std::uint64_t initialFnv64() { return 0xcbf29ce484222325ull; }

  static std::uint64_t updateFnv64(std::uint64_t state, std::uint64_t value, std::uint32_t width) {
    static constexpr std::uint64_t kFnvPrime = 0x100000001b3ull;
    std::uint32_t bytes = (width + 7u) / 8u;
    if (bytes == 0u) bytes = 8u;
    if (bytes > 8u) bytes = 8u;
    for (std::uint32_t byte = 0; byte < bytes; ++byte) {
      state ^= (value >> (byte * 8u)) & 0xffull;
      state *= kFnvPrime;
    }
    return state;
  }

  static std::string formatFnv64(std::uint64_t digest) {
    std::ostringstream oss;
    oss << "fnv64:" << std::hex << std::nouppercase << std::setfill('0') << std::setw(16) << digest;
    return oss.str();
  }

  bool matchesDigest(std::uint64_t digest) const {
    return policy_.signature == formatFnv64(digest);
  }

 private:
  pyc::cpp::Pycstb4ScoreboardPolicy policy_;
};

class SampledScoreboardPolicy final : public ScoreboardPolicyRuntime {
 public:
  explicit SampledScoreboardPolicy(pyc::cpp::Pycstb4ScoreboardPolicy policy)
      : policy_(std::move(policy)) {
    if (policy_.kind != "sampled") {
      throw std::runtime_error("SampledScoreboardPolicy requires kind=sampled");
    }
    if (policy_.sample_period == 0) {
      throw std::runtime_error("SampledScoreboardPolicy requires sample_period > 0");
    }
  }

  const pyc::cpp::Pycstb4ScoreboardPolicy& metadata() const override { return policy_; }

  PolicyDecision decision(std::uint64_t index) const override {
    return PolicyDecision{/*should_check=*/(index % policy_.sample_period) == 0, /*stop_on_mismatch=*/false};
  }

 private:
  pyc::cpp::Pycstb4ScoreboardPolicy policy_;
};

class OrderedPayloadScoreboardPolicy final : public ScoreboardPolicyRuntime {
 public:
  explicit OrderedPayloadScoreboardPolicy(pyc::cpp::Pycstb4ScoreboardPolicy policy)
      : policy_(std::move(policy)) {
    if (policy_.kind != "ordered_payload" && policy_.kind != "ordered") {
      throw std::runtime_error("OrderedPayloadScoreboardPolicy requires kind=ordered_payload");
    }
  }

  const pyc::cpp::Pycstb4ScoreboardPolicy& metadata() const override { return policy_; }

  PolicyDecision decision(std::uint64_t) const override {
    return PolicyDecision{/*should_check=*/true, /*stop_on_mismatch=*/(policy_.flags & 1ull) != 0ull};
  }

 private:
  pyc::cpp::Pycstb4ScoreboardPolicy policy_;
};

class ScoreboardPolicyRegistry {
 public:
  static bool supports(const pyc::cpp::Pycstb4ScoreboardPolicy& policy) {
    return policy.kind == "ordered_payload" || policy.kind == "ordered" || policy.kind == "signature" || policy.kind == "sampled";
  }

  static std::unique_ptr<ScoreboardPolicyRuntime> create(pyc::cpp::Pycstb4ScoreboardPolicy policy) {
    if (policy.kind == "ordered_payload" || policy.kind == "ordered") {
      return std::unique_ptr<ScoreboardPolicyRuntime>(new OrderedPayloadScoreboardPolicy(std::move(policy)));
    }
    if (policy.kind == "signature") {
      return std::unique_ptr<ScoreboardPolicyRuntime>(new SignatureScoreboardPolicy(std::move(policy)));
    }
    if (policy.kind == "sampled") {
      return std::unique_ptr<ScoreboardPolicyRuntime>(new SampledScoreboardPolicy(std::move(policy)));
    }
    throw std::runtime_error("unsupported scoreboard policy kind: " + policy.kind);
  }
};

class InstructionSource {
 public:
  virtual ~InstructionSource() = default;
  virtual std::uint64_t count() const = 0;
  virtual const pyc::cpp::Pycstb4InstructionStream& metadata() const = 0;
  virtual InstructionWord at(std::uint64_t instruction_index) const = 0;
};

class InlineInstructionStreamSource final : public InstructionSource {
 public:
  explicit InlineInstructionStreamSource(pyc::cpp::Pycstb4InstructionStream stream)
      : stream_(std::move(stream)) {
    if (stream_.instruction_width == 0 || stream_.instruction_width > 64) {
      throw std::runtime_error("inline instruction stream v0 supports instruction width 1..64");
    }
    const std::uint64_t bytes_per_instruction = (stream_.instruction_width + 7u) / 8u;
    if (bytes_per_instruction == 0 || bytes_per_instruction > 8) {
      throw std::runtime_error("inline instruction stream v0 supports up to 8 bytes per instruction");
    }
    if (stream_.payload.size() < stream_.count * bytes_per_instruction) {
      throw std::runtime_error("inline instruction stream payload is truncated");
    }
  }

  std::uint64_t count() const override { return stream_.count; }
  const pyc::cpp::Pycstb4InstructionStream& metadata() const override { return stream_; }

  InstructionWord at(std::uint64_t instruction_index) const override {
    if (instruction_index >= stream_.count) {
      throw std::out_of_range("instruction index out of range");
    }
    const std::uint64_t bytes_per_instruction = (stream_.instruction_width + 7u) / 8u;
    const std::uint64_t offset = instruction_index * bytes_per_instruction;
    std::uint64_t value = 0;
    for (std::uint64_t idx = 0; idx < bytes_per_instruction; ++idx) {
      value |= static_cast<std::uint64_t>(stream_.payload[static_cast<std::size_t>(offset + idx)]) << (8u * idx);
    }
    if (stream_.instruction_width < 64u) {
      value &= ((1ull << stream_.instruction_width) - 1ull);
    }
    return InstructionWord{instruction_index, value, stream_.instruction_width};
  }

 private:
  pyc::cpp::Pycstb4InstructionStream stream_;
};

class ExternalRawInstructionSource {
 public:
  ExternalRawInstructionSource(pyc::cpp::Pycstb4ExternalStreamSource source, std::uint32_t instruction_width)
      : source_(std::move(source)), instruction_width_(instruction_width) {
    if (source_.format != "raw_le32_instruction_stream" && source_.format != "raw_le64_instruction_stream" && source_.format != "raw_instruction_stream") {
      throw std::runtime_error("unsupported external instruction stream format: " + source_.format);
    }
    if (instruction_width_ == 0 || instruction_width_ > 64) {
      throw std::runtime_error("external raw instruction source supports instruction width 1..64");
    }
    bytes_per_instruction_ = (instruction_width_ + 7u) / 8u;
    if (bytes_per_instruction_ == 0 || bytes_per_instruction_ > 8) {
      throw std::runtime_error("external raw instruction source supports up to 8 bytes per instruction");
    }
    if (source_.byte_size % bytes_per_instruction_ != 0) {
      throw std::runtime_error("external raw instruction byte_size is not aligned to instruction width");
    }
    count_ = source_.byte_size / bytes_per_instruction_;
  }

  std::uint64_t count() const { return count_; }
  const pyc::cpp::Pycstb4ExternalStreamSource& metadata() const { return source_; }

  InstructionWord at(std::uint64_t instruction_index) const {
    if (instruction_index >= count_) {
      throw std::out_of_range("external instruction index out of range");
    }
    const std::uint64_t offset = source_.offset + instruction_index * bytes_per_instruction_;
    ensureCached(offset);
    std::uint64_t value = 0;
    const std::uint64_t cache_index = offset - cache_start_;
    for (std::uint64_t idx = 0; idx < bytes_per_instruction_; ++idx) {
      value |= static_cast<std::uint64_t>(cache_.at(static_cast<std::size_t>(cache_index + idx))) << (8u * idx);
    }
    if (instruction_width_ < 64u) {
      value &= ((1ull << instruction_width_) - 1ull);
    }
    return InstructionWord{instruction_index, value, instruction_width_};
  }

 private:
  void ensureCached(std::uint64_t absolute_offset) const {
    if (cache_valid_ && absolute_offset >= cache_start_ && absolute_offset + bytes_per_instruction_ <= cache_start_ + cache_.size()) {
      return;
    }
    if (!file_.is_open()) {
      file_.open(source_.path, std::ios::binary);
      if (!file_) {
        throw std::runtime_error("failed to open external instruction stream: " + source_.path);
      }
    }
    const std::uint64_t default_chunk = source_.chunk_size ? source_.chunk_size : 4096ull;
    const std::uint64_t chunk_size = default_chunk < bytes_per_instruction_ ? bytes_per_instruction_ : default_chunk;
    const std::uint64_t relative = absolute_offset - source_.offset;
    cache_start_ = source_.offset + (relative / chunk_size) * chunk_size;
    std::uint64_t remaining = source_.offset + source_.byte_size - cache_start_;
    const std::uint64_t read_size = remaining < chunk_size ? remaining : chunk_size;
    cache_.assign(static_cast<std::size_t>(read_size), 0);
    file_.clear();
    file_.seekg(static_cast<std::streamoff>(cache_start_), std::ios::beg);
    if (!file_) {
      throw std::runtime_error("failed to seek external instruction stream");
    }
    if (read_size != 0) {
      file_.read(reinterpret_cast<char *>(cache_.data()), static_cast<std::streamsize>(read_size));
      if (!file_) {
        throw std::runtime_error("failed to read external instruction stream");
      }
    }
    cache_valid_ = true;
  }

  pyc::cpp::Pycstb4ExternalStreamSource source_;
  std::uint32_t instruction_width_ = 0;
  std::uint64_t bytes_per_instruction_ = 0;
  std::uint64_t count_ = 0;
  mutable std::ifstream file_;
  mutable std::uint64_t cache_start_ = 0;
  mutable std::vector<std::uint8_t> cache_;
  mutable bool cache_valid_ = false;
};

class TransactionSource {
 public:
  virtual ~TransactionSource() = default;
  virtual std::uint64_t count() const = 0;
  virtual std::vector<std::uint32_t> defaultPayloadPorts() const = 0;
  virtual pyc::actor_v0::Transaction atWithPorts(
      std::uint64_t transaction_index,
      const std::vector<std::uint32_t>& payload_ports) const = 0;

  pyc::actor_v0::Transaction at(std::uint64_t transaction_index) const {
    return atWithPorts(transaction_index, defaultPayloadPorts());
  }
};

class ExternalRawPayloadTransactionSource final : public TransactionSource {
 public:
  ExternalRawPayloadTransactionSource(
      pyc::cpp::Pycstb4ExternalStreamSource source,
      std::vector<std::uint32_t> payload_ports,
      std::uint32_t payload_width)
      : source_(std::move(source)), payload_ports_(std::move(payload_ports)), payload_width_(payload_width) {
    if (!supportsFormat(source_.format)) {
      throw std::runtime_error("unsupported external payload stream format: " + source_.format);
    }
    if (payload_ports_.size() != 1u) {
      throw std::runtime_error("external raw payload transaction source v0 supports exactly one payload port");
    }
    if (payload_width_ == 0 || payload_width_ > 32u) {
      throw std::runtime_error("external raw payload transaction source v0 supports payload width 1..32");
    }
    bytes_per_payload_ = 4u;
    if (source_.byte_size % bytes_per_payload_ != 0) {
      throw std::runtime_error("external raw payload byte_size is not aligned to u32 payload words");
    }
    count_ = source_.byte_size / bytes_per_payload_;
  }

  static bool supportsFormat(const std::string& format) {
    return format == "mock_raw_u32_le" ||
           format == "raw_u32_le" ||
           format == "raw_le32_payload_stream" ||
           format == "raw_le32_transaction_stream";
  }

  std::uint64_t count() const override { return count_; }
  std::vector<std::uint32_t> defaultPayloadPorts() const override { return payload_ports_; }

  pyc::actor_v0::Transaction atWithPorts(
      std::uint64_t transaction_index,
      const std::vector<std::uint32_t>& payload_ports) const override {
    if (transaction_index >= count_) {
      throw std::out_of_range("external raw payload transaction index out of range");
    }
    if (payload_ports.size() != 1u) {
      throw std::runtime_error("external raw payload transaction source v0 supports exactly one requested payload port");
    }
    const std::uint64_t offset = source_.offset + transaction_index * bytes_per_payload_;
    ensureCached(offset);
    const std::uint64_t cache_index = offset - cache_start_;
    std::uint64_t value = 0;
    for (std::uint64_t idx = 0; idx < bytes_per_payload_; ++idx) {
      value |= static_cast<std::uint64_t>(cache_.at(static_cast<std::size_t>(cache_index + idx))) << (8u * idx);
    }
    if (payload_width_ < 32u) {
      value &= ((1ull << payload_width_) - 1ull);
    }
    pyc::actor_v0::Transaction tx;
    tx.payload.push_back(pyc::actor_v0::PayloadWord{payload_ports.front(), value});
    return tx;
  }

 private:
  void ensureCached(std::uint64_t absolute_offset) const {
    if (cache_valid_ && absolute_offset >= cache_start_ && absolute_offset + bytes_per_payload_ <= cache_start_ + cache_.size()) {
      return;
    }
    if (!file_.is_open()) {
      file_.open(source_.path, std::ios::binary);
      if (!file_) {
        throw std::runtime_error("failed to open external payload stream: " + source_.path);
      }
    }
    const std::uint64_t default_chunk = source_.chunk_size ? source_.chunk_size : 4096ull;
    const std::uint64_t chunk_size = default_chunk < bytes_per_payload_ ? bytes_per_payload_ : default_chunk;
    const std::uint64_t relative = absolute_offset - source_.offset;
    cache_start_ = source_.offset + (relative / chunk_size) * chunk_size;
    std::uint64_t remaining = source_.offset + source_.byte_size - cache_start_;
    const std::uint64_t read_size = remaining < chunk_size ? remaining : chunk_size;
    cache_.assign(static_cast<std::size_t>(read_size), 0);
    file_.clear();
    file_.seekg(static_cast<std::streamoff>(cache_start_), std::ios::beg);
    if (!file_) {
      throw std::runtime_error("failed to seek external payload stream");
    }
    if (read_size != 0) {
      file_.read(reinterpret_cast<char *>(cache_.data()), static_cast<std::streamsize>(read_size));
      if (!file_) {
        throw std::runtime_error("failed to read external payload stream");
      }
    }
    cache_valid_ = true;
  }

  pyc::cpp::Pycstb4ExternalStreamSource source_;
  std::vector<std::uint32_t> payload_ports_;
  std::uint32_t payload_width_ = 0;
  std::uint64_t bytes_per_payload_ = 0;
  std::uint64_t count_ = 0;
  mutable std::ifstream file_;
  mutable std::uint64_t cache_start_ = 0;
  mutable std::vector<std::uint8_t> cache_;
  mutable bool cache_valid_ = false;
};

class LcgPayloadTransactionSource final : public TransactionSource {
 public:
  explicit LcgPayloadTransactionSource(pyc::cpp::Pycstb4SeededWorkloadGenerator generator)
      : generator_(std::move(generator)) {
    if (generator_.generator_id != "lcg_payload_v0") {
      throw std::runtime_error("unsupported seeded workload generator_id: " + generator_.generator_id);
    }
    if (generator_.output_ports.empty()) {
      throw std::runtime_error("seeded workload generator requires at least one output port");
    }
  }

  std::uint64_t count() const override { return generator_.count; }
  const pyc::cpp::Pycstb4SeededWorkloadGenerator& metadata() const { return generator_; }
  std::vector<std::uint32_t> defaultPayloadPorts() const override { return generator_.output_ports; }

  pyc::actor_v0::Transaction atWithPorts(
      std::uint64_t transaction_index,
      const std::vector<std::uint32_t>& payload_ports) const override {
    if (transaction_index >= generator_.count) {
      throw std::out_of_range("seeded workload transaction index out of range");
    }
    const std::uint64_t logical_index = generator_.start_index + transaction_index;
    pyc::actor_v0::Transaction tx;
    tx.payload.reserve(payload_ports.size());
    for (std::size_t port_index = 0; port_index < payload_ports.size(); ++port_index) {
      tx.payload.push_back(pyc::actor_v0::PayloadWord{payload_ports[port_index], valueFor(logical_index, port_index)});
    }
    return tx;
  }

 private:
  static std::uint64_t parseUnsignedAfterKey(const std::string& json, const std::string& key, std::uint64_t default_value) {
    const std::string needle = "\"" + key + "\"";
    std::size_t pos = json.find(needle);
    if (pos == std::string::npos) return default_value;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return default_value;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\"')) ++pos;
    int base = 10;
    if (pos + 2 <= json.size() && json[pos] == '0' && (json[pos + 1] == 'x' || json[pos + 1] == 'X')) {
      base = 16;
      pos += 2;
    }
    std::uint64_t value = 0;
    bool any = false;
    for (; pos < json.size(); ++pos) {
      const char c = json[pos];
      std::uint64_t digit = 0;
      if (c >= '0' && c <= '9') {
        digit = static_cast<std::uint64_t>(c - '0');
      } else if (base == 16 && c >= 'a' && c <= 'f') {
        digit = static_cast<std::uint64_t>(10 + c - 'a');
      } else if (base == 16 && c >= 'A' && c <= 'F') {
        digit = static_cast<std::uint64_t>(10 + c - 'A');
      } else {
        break;
      }
      if (digit >= static_cast<std::uint64_t>(base)) break;
      value = value * static_cast<std::uint64_t>(base) + digit;
      any = true;
    }
    return any ? value : default_value;
  }

  std::uint64_t valueFor(std::uint64_t logical_index, std::size_t port_index) const {
    // v0 intentionally mirrors the synthetic ready-valid payload formula while
    // allowing a seed and multi-port offset. This is software workload logic,
    // not hardware elaboration logic.
    static constexpr std::uint64_t kPortSalt = 0x9E3779B97F4A7C15ull;
    const std::uint64_t multiplier = parseUnsignedAfterKey(generator_.constraints_json, "multiplier", 0x45D9F3Bull);
    const std::uint64_t data_width = parseUnsignedAfterKey(generator_.constraints_json, "data_width", 64);
    std::uint64_t value =
        (logical_index + 1ull) * multiplier + generator_.seed + static_cast<std::uint64_t>(port_index) * kPortSalt;
    if (data_width < 64) {
      value &= ((1ull << data_width) - 1ull);
    }
    return value;
  }

  pyc::cpp::Pycstb4SeededWorkloadGenerator generator_;
};

class PayloadTableTransactionSource final : public TransactionSource {
 public:
  explicit PayloadTableTransactionSource(pyc::cpp::Pycstb4ActorPayloadTable table)
      : table_(std::move(table)) {
    if (table_.payload_word_count != 1u) {
      throw std::runtime_error("payload table transaction source v0 supports one word per payload port");
    }
    if (table_.payload_ports.empty()) {
      throw std::runtime_error("payload table transaction source requires at least one payload port");
    }
    const std::uint64_t expected_words =
        static_cast<std::uint64_t>(table_.transaction_count) *
        static_cast<std::uint64_t>(table_.payload_ports.size()) *
        static_cast<std::uint64_t>(table_.payload_word_count);
    if (table_.words.size() < expected_words) {
      throw std::runtime_error("payload table word data is truncated");
    }
  }

  std::uint64_t count() const override { return table_.transaction_count; }
  std::vector<std::uint32_t> defaultPayloadPorts() const override { return table_.payload_ports; }

  pyc::actor_v0::Transaction atWithPorts(
      std::uint64_t transaction_index,
      const std::vector<std::uint32_t>& payload_ports) const override {
    if (transaction_index >= table_.transaction_count) {
      throw std::out_of_range("payload table transaction index out of range");
    }
    if (payload_ports.size() != table_.payload_ports.size()) {
      throw std::runtime_error("payload table requested port count does not match table shape");
    }
    pyc::actor_v0::Transaction tx;
    tx.payload.reserve(payload_ports.size());
    for (std::size_t port_index = 0; port_index < payload_ports.size(); ++port_index) {
      const std::size_t index =
          static_cast<std::size_t>(transaction_index) * table_.payload_ports.size() * table_.payload_word_count +
          port_index * table_.payload_word_count;
      tx.payload.push_back(pyc::actor_v0::PayloadWord{payload_ports[port_index], table_.words.at(index)});
    }
    return tx;
  }

 private:
  pyc::cpp::Pycstb4ActorPayloadTable table_;
};

class WorkloadGeneratorRegistry {
 public:
  static bool supports(const pyc::cpp::Pycstb4SeededWorkloadGenerator& generator) {
    return generator.generator_id == "lcg_payload_v0";
  }

  static std::unique_ptr<TransactionSource> create(pyc::cpp::Pycstb4SeededWorkloadGenerator generator) {
    if (generator.generator_id == "lcg_payload_v0") {
      return std::unique_ptr<TransactionSource>(new LcgPayloadTransactionSource(std::move(generator)));
    }
    throw std::runtime_error("unsupported seeded workload generator_id: " + generator.generator_id);
  }
};

class WorkloadSourceRegistry {
 public:
  static bool supportsInstructionStream(const pyc::cpp::Pycstb4InstructionStream& stream) {
    return stream.encoding == "raw_le32_inline" || stream.encoding == "raw_le64_inline" || stream.encoding == "raw_inline";
  }

  static std::unique_ptr<InstructionSource> createInstructionSource(pyc::cpp::Pycstb4InstructionStream stream) {
    if (supportsInstructionStream(stream)) {
      return std::unique_ptr<InstructionSource>(new InlineInstructionStreamSource(std::move(stream)));
    }
    throw std::runtime_error("unsupported instruction stream encoding: " + stream.encoding);
  }

  static ExternalRawInstructionSource createExternalInstructionSource(
      pyc::cpp::Pycstb4ExternalStreamSource source,
      std::uint32_t instruction_width) {
    return ExternalRawInstructionSource(std::move(source), instruction_width);
  }

  static bool supportsExternalPayloadSource(const pyc::cpp::Pycstb4ExternalStreamSource& source) {
    return ExternalRawPayloadTransactionSource::supportsFormat(source.format);
  }

  static std::unique_ptr<TransactionSource> createTransactionSource(pyc::cpp::Pycstb4SeededWorkloadGenerator generator) {
    return WorkloadGeneratorRegistry::create(std::move(generator));
  }

  static std::unique_ptr<TransactionSource> createTransactionSource(pyc::cpp::Pycstb4ActorPayloadTable table) {
    return std::unique_ptr<TransactionSource>(new PayloadTableTransactionSource(std::move(table)));
  }

  static std::unique_ptr<TransactionSource> createTransactionSource(
      pyc::cpp::Pycstb4ExternalStreamSource source,
      std::vector<std::uint32_t> payload_ports,
      std::uint32_t payload_width) {
    return std::unique_ptr<TransactionSource>(
        new ExternalRawPayloadTransactionSource(std::move(source), std::move(payload_ports), payload_width));
  }
};

class GeneratedTransactionSource {
 public:
  explicit GeneratedTransactionSource(pyc::cpp::Pycstb4SeededWorkloadGenerator generator)
      : source_(WorkloadGeneratorRegistry::create(std::move(generator))) {}

  explicit GeneratedTransactionSource(pyc::cpp::Pycstb4ActorPayloadTable table)
      : source_(WorkloadSourceRegistry::createTransactionSource(std::move(table))) {}

  GeneratedTransactionSource(
      pyc::cpp::Pycstb4ExternalStreamSource source,
      std::vector<std::uint32_t> payload_ports,
      std::uint32_t payload_width)
      : source_(WorkloadSourceRegistry::createTransactionSource(std::move(source), std::move(payload_ports), payload_width)) {}

  GeneratedTransactionSource(GeneratedTransactionSource&&) noexcept = default;
  GeneratedTransactionSource& operator=(GeneratedTransactionSource&&) noexcept = default;
  GeneratedTransactionSource(const GeneratedTransactionSource&) = delete;
  GeneratedTransactionSource& operator=(const GeneratedTransactionSource&) = delete;

  std::uint64_t count() const { return source_->count(); }
  std::vector<std::uint32_t> defaultPayloadPorts() const { return source_->defaultPayloadPorts(); }

  pyc::actor_v0::Transaction at(std::uint64_t transaction_index) const {
    return source_->at(transaction_index);
  }

  pyc::actor_v0::Transaction atWithPorts(
      std::uint64_t transaction_index,
      const std::vector<std::uint32_t>& payload_ports) const {
    return source_->atWithPorts(transaction_index, payload_ports);
  }

  std::vector<pyc::actor_v0::Transaction> materialize() const {
    std::vector<pyc::actor_v0::Transaction> out;
    out.reserve(static_cast<std::size_t>(count()));
    for (std::uint64_t idx = 0; idx < count(); ++idx) {
      out.push_back(at(idx));
    }
    return out;
  }

  std::vector<pyc::actor_v0::Transaction> materializeWithPorts(const std::vector<std::uint32_t>& payload_ports) const {
    std::vector<pyc::actor_v0::Transaction> out;
    out.reserve(static_cast<std::size_t>(count()));
    for (std::uint64_t idx = 0; idx < count(); ++idx) {
      out.push_back(atWithPorts(idx, payload_ports));
    }
    return out;
  }

 private:
  std::unique_ptr<TransactionSource> source_;
};

inline const pyc::cpp::Pycstb4SeededWorkloadGenerator* findSeededGenerator(
    const pyc::cpp::Pycstb4Schedule& schedule,
    const std::string& generator_id) {
  for (const auto& generator : schedule.seeded_workload_generators) {
    if (generator.generator_id == generator_id) {
      return &generator;
    }
  }
  return nullptr;
}

inline const pyc::cpp::Pycstb4InstructionStream* findInstructionStream(
    const pyc::cpp::Pycstb4Schedule& schedule,
    const std::string& isa) {
  for (const auto& stream : schedule.instruction_streams) {
    if (stream.isa == isa) {
      return &stream;
    }
  }
  return nullptr;
}

inline const pyc::cpp::Pycstb4ExternalStreamSource* findExternalStreamSource(
    const pyc::cpp::Pycstb4Schedule& schedule,
    const std::string& format) {
  for (const auto& source : schedule.external_stream_sources) {
    if (source.format == format) {
      return &source;
    }
  }
  return nullptr;
}

struct SectionRuntimeSummary {
  std::uint64_t instruction_stream_count = 0;
  std::uint64_t instruction_word_count = 0;
  std::uint64_t external_stream_source_count = 0;
  std::uint64_t external_declared_bytes = 0;
  std::uint64_t seeded_generator_count = 0;
  std::uint64_t seeded_transaction_count = 0;
  std::uint64_t actor_payload_table_count = 0;
  std::uint64_t actor_payload_transaction_count = 0;
  std::uint64_t scoreboard_policy_count = 0;
  std::uint64_t metadata_only_policy_count = 0;
  std::uint64_t unsupported_or_invalid_count = 0;
  std::vector<std::string> messages;
};

inline SectionRuntimeSummary summarizePycstb4RuntimeSections(const pyc::cpp::Pycstb4Schedule& schedule) {
  SectionRuntimeSummary summary;
  summary.instruction_stream_count = static_cast<std::uint64_t>(schedule.instruction_streams.size());
  summary.external_stream_source_count = static_cast<std::uint64_t>(schedule.external_stream_sources.size());
  summary.seeded_generator_count = static_cast<std::uint64_t>(schedule.seeded_workload_generators.size());
  summary.actor_payload_table_count = static_cast<std::uint64_t>(schedule.actor_payload_tables.size());
  summary.scoreboard_policy_count = static_cast<std::uint64_t>(schedule.scoreboard_policies.size());

  for (const auto& stream : schedule.instruction_streams) {
    summary.instruction_word_count += stream.count;
    if (!WorkloadSourceRegistry::supportsInstructionStream(stream)) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back("instruction_stream metadata-only: unsupported encoding=" + stream.encoding);
      continue;
    }
    try {
      const auto source = WorkloadSourceRegistry::createInstructionSource(stream);
      if (source->count() != stream.count) {
        ++summary.unsupported_or_invalid_count;
        summary.messages.push_back("instruction_stream count mismatch after runtime construction");
      }
    } catch (const std::exception& e) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back(std::string("instruction_stream invalid: ") + e.what());
    }
  }

  for (const auto& source : schedule.external_stream_sources) {
    summary.external_declared_bytes += source.byte_size;
    if (
        source.format != "raw_le32_instruction_stream" &&
        source.format != "raw_le64_instruction_stream" &&
        source.format != "raw_instruction_stream" &&
        !WorkloadSourceRegistry::supportsExternalPayloadSource(source)) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back("external_stream_source metadata-only: unsupported format=" + source.format);
      continue;
    }
    if (WorkloadSourceRegistry::supportsExternalPayloadSource(source)) {
      try {
        const auto payload_source = WorkloadSourceRegistry::createTransactionSource(source, std::vector<std::uint32_t>{1u}, 32u);
        (void)payload_source->count();
      } catch (const std::exception& e) {
        ++summary.unsupported_or_invalid_count;
        summary.messages.push_back(std::string("external_stream_source invalid payload source: ") + e.what());
      }
    }
  }

  for (const auto& generator : schedule.seeded_workload_generators) {
    summary.seeded_transaction_count += generator.count;
    if (!WorkloadGeneratorRegistry::supports(generator)) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back("seeded_workload_generator metadata-only: unsupported generator_id=" + generator.generator_id);
      continue;
    }
    try {
      const auto source = WorkloadSourceRegistry::createTransactionSource(generator);
      if (source->count() != generator.count) {
        ++summary.unsupported_or_invalid_count;
        summary.messages.push_back("seeded_workload_generator count mismatch after runtime construction");
      }
    } catch (const std::exception& e) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back(std::string("seeded_workload_generator invalid: ") + e.what());
    }
  }

  for (const auto& table : schedule.actor_payload_tables) {
    summary.actor_payload_transaction_count += table.transaction_count;
    try {
      const auto source = WorkloadSourceRegistry::createTransactionSource(table);
      if (source->count() != table.transaction_count) {
        ++summary.unsupported_or_invalid_count;
        summary.messages.push_back("actor_payload_table count mismatch after runtime construction");
      }
    } catch (const std::exception& e) {
      ++summary.unsupported_or_invalid_count;
      summary.messages.push_back(std::string("actor_payload_table invalid: ") + e.what());
    }
  }

  for (const auto& policy : schedule.scoreboard_policies) {
    if (ScoreboardPolicyRegistry::supports(policy)) {
      try {
        const auto runtime = ScoreboardPolicyRegistry::create(policy);
        (void)runtime;
      } catch (const std::exception& e) {
        ++summary.unsupported_or_invalid_count;
        summary.messages.push_back(std::string("scoreboard_policy invalid: ") + e.what());
      }
    } else {
      ++summary.metadata_only_policy_count;
      summary.messages.push_back("scoreboard_policy metadata-only: kind=" + policy.kind);
    }
  }

  return summary;
}

struct GeneratedReadyValidRuntimeResult {
  std::uint64_t cycles = 0;
  std::uint64_t expected_count = 0;
  std::uint64_t actual_count = 0;
  std::uint64_t mismatch_count = 0;
  std::vector<std::string> messages;
};

class GeneratedReadyValidRuntime {
 public:
  GeneratedReadyValidRuntime(
      pyc::actor_v0::PortIo io,
      GeneratedTransactionSource source,
      std::uint32_t source_valid_port,
      std::uint32_t source_ready_port,
      std::vector<std::uint32_t> source_payload_ports,
      std::uint32_t sink_valid_port,
      std::uint32_t sink_ready_port,
      std::vector<std::uint32_t> sink_payload_ports,
      pyc::actor_v0::ReadyPattern sink_ready_pattern,
      std::uint64_t start_cycle,
      std::uint64_t end_cycle)
      : io_(std::move(io)),
        source_(std::move(source)),
        source_valid_port_(source_valid_port),
        source_ready_port_(source_ready_port),
        source_payload_ports_(std::move(source_payload_ports)),
        sink_valid_port_(sink_valid_port),
        sink_ready_port_(sink_ready_port),
        sink_payload_ports_(std::move(sink_payload_ports)),
        sink_ready_pattern_(sink_ready_pattern),
        start_cycle_(start_cycle),
        end_cycle_(end_cycle) {
    if (!io_.read || !io_.write) {
      throw std::invalid_argument("PortIo read/write callbacks must be set");
    }
  }

  template <typename EvalFn>
  GeneratedReadyValidRuntimeResult run(std::uint64_t run_start_cycle, std::uint64_t run_end_cycle, EvalFn&& eval) {
    for (std::uint64_t cycle = run_start_cycle; cycle <= run_end_cycle; ++cycle) {
      sourcePreCycle(cycle);
      sinkPreCycle(cycle);
      eval(cycle);
      sourcePostCycle(cycle);
      sinkPostCycle(cycle);
    }
    GeneratedReadyValidRuntimeResult result;
    result.cycles = run_end_cycle >= run_start_cycle ? run_end_cycle - run_start_cycle + 1 : 0;
    result.expected_count = source_.count();
    result.actual_count = actual_count_;
    result.mismatch_count = mismatch_count_;
    result.messages = messages_;
    if (actual_count_ < source_.count()) {
      result.mismatch_count += source_.count() - actual_count_;
      result.messages.push_back("missing " + std::to_string(source_.count() - actual_count_) + " transaction(s)");
    }
    return result;
  }

 private:
  void sourcePreCycle(std::uint64_t cycle) {
    if (cycle < start_cycle_ || cycle > end_cycle_ || source_index_ >= source_.count()) {
      io_.write(source_valid_port_, 0);
      return;
    }
    io_.write(source_valid_port_, 1);
    const auto tx = source_.atWithPorts(source_index_, source_payload_ports_);
    for (const auto& word : tx.payload) {
      io_.write(word.port, word.value);
    }
  }

  void sourcePostCycle(std::uint64_t cycle) {
    if (cycle < start_cycle_ || cycle > end_cycle_ || source_index_ >= source_.count()) {
      return;
    }
    if (io_.read(source_ready_port_) != 0) {
      ++source_index_;
    }
  }

  void sinkPreCycle(std::uint64_t cycle) {
    if (cycle < start_cycle_ || cycle > end_cycle_) {
      io_.write(sink_ready_port_, 0);
      return;
    }
    io_.write(sink_ready_port_, sink_ready_pattern_.valueAt(cycle, start_cycle_, end_cycle_));
  }

  void sinkPostCycle(std::uint64_t cycle) {
    if (cycle < start_cycle_ || cycle > end_cycle_) {
      return;
    }
    if (io_.read(sink_valid_port_) == 0 || io_.read(sink_ready_port_) == 0) {
      return;
    }
    const std::uint64_t index = actual_count_;
    ++actual_count_;
    if (index >= source_.count()) {
      ++mismatch_count_;
      messages_.push_back("extra actual transaction at cycle " + std::to_string(cycle));
      return;
    }
    const auto expected = source_.atWithPorts(index, sink_payload_ports_);
    for (std::size_t payload_index = 0; payload_index < sink_payload_ports_.size(); ++payload_index) {
      const std::uint32_t port = sink_payload_ports_[payload_index];
      const std::uint64_t actual = io_.read(port);
      const std::uint64_t want = expected.payload[payload_index].value;
      if (actual != want) {
        ++mismatch_count_;
        messages_.push_back(
            "mismatch index=" + std::to_string(index) +
            " port=" + std::to_string(port) +
            " cycle=" + std::to_string(cycle) +
            " expected=" + std::to_string(want) +
            " actual=" + std::to_string(actual));
      }
    }
  }

  pyc::actor_v0::PortIo io_;
  GeneratedTransactionSource source_;
  std::uint32_t source_valid_port_ = 0;
  std::uint32_t source_ready_port_ = 0;
  std::vector<std::uint32_t> source_payload_ports_;
  std::uint32_t sink_valid_port_ = 0;
  std::uint32_t sink_ready_port_ = 0;
  std::vector<std::uint32_t> sink_payload_ports_;
  pyc::actor_v0::ReadyPattern sink_ready_pattern_;
  std::uint64_t start_cycle_ = 0;
  std::uint64_t end_cycle_ = 0;
  std::uint64_t source_index_ = 0;
  std::uint64_t actual_count_ = 0;
  std::uint64_t mismatch_count_ = 0;
  std::vector<std::string> messages_;
};

}  // namespace pyc::workload_v0
