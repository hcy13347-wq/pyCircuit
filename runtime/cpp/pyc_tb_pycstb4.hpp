#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <cpp/pyc_tb_runtime_loop.hpp>

namespace pyc::cpp {

enum : std::uint16_t {
  kPycstb4SectionStringTable = 1,
  kPycstb4SectionPortTable = 2,
  kPycstb4SectionEventTable = 3,
  kPycstb4SectionFrameTable = 4,
  kPycstb4SectionPatternTable = 5,
  kPycstb4SectionActorBundle = 16,
  kPycstb4SectionActorPayloadBlob = 17,
  kPycstb4SectionActorPayloadTable = 18,
  kPycstb4SectionInstructionStream = 19,
  kPycstb4SectionSeededWorkloadGenerator = 20,
  kPycstb4SectionExternalStreamSource = 21,
  kPycstb4SectionScoreboardPolicy = 22,
};

struct Pycstb4SectionCapability {
  std::uint16_t kind = 0;
  const char *name = "";
  bool required = false;
  bool experimental = false;
};

inline const char *pycstb4SectionName(std::uint16_t kind) {
  switch (kind) {
    case kPycstb4SectionStringTable: return "string_table";
    case kPycstb4SectionPortTable: return "port_table";
    case kPycstb4SectionEventTable: return "event_table";
    case kPycstb4SectionFrameTable: return "frame_table";
    case kPycstb4SectionPatternTable: return "pattern_table";
    case kPycstb4SectionActorBundle: return "actor_bundle";
    case kPycstb4SectionActorPayloadBlob: return "actor_payload_blob";
    case kPycstb4SectionActorPayloadTable: return "actor_payload_table";
    case kPycstb4SectionInstructionStream: return "instruction_stream";
    case kPycstb4SectionSeededWorkloadGenerator: return "seeded_workload_generator";
    case kPycstb4SectionExternalStreamSource: return "external_stream_source";
    case kPycstb4SectionScoreboardPolicy: return "scoreboard_policy";
    default: return "unknown";
  }
}

inline const std::vector<Pycstb4SectionCapability> &pycstb4SupportedSectionCapabilities() {
  static const std::vector<Pycstb4SectionCapability> caps = {
      {kPycstb4SectionStringTable, "string_table", true, false},
      {kPycstb4SectionPortTable, "port_table", true, false},
      {kPycstb4SectionEventTable, "event_table", true, false},
      {kPycstb4SectionFrameTable, "frame_table", true, false},
      {kPycstb4SectionPatternTable, "pattern_table", false, false},
      {kPycstb4SectionActorBundle, "actor_bundle", false, true},
      {kPycstb4SectionActorPayloadBlob, "actor_payload_blob", false, true},
      {kPycstb4SectionActorPayloadTable, "actor_payload_table", false, true},
      {kPycstb4SectionInstructionStream, "instruction_stream", false, true},
      {kPycstb4SectionSeededWorkloadGenerator, "seeded_workload_generator", false, true},
      {kPycstb4SectionExternalStreamSource, "external_stream_source", false, true},
      {kPycstb4SectionScoreboardPolicy, "scoreboard_policy", false, true},
  };
  return caps;
}

inline bool pycstb4SectionSupported(std::uint16_t kind) {
  for (const auto &cap : pycstb4SupportedSectionCapabilities()) {
    if (cap.kind == kind) return true;
  }
  return false;
}

struct Pycstb4SectionInfo {
  std::uint16_t kind = 0;
  std::uint16_t flags = 0;
  std::uint64_t offset = 0;
  std::uint64_t size = 0;
  std::uint64_t count = 0;
};

struct Pycstb4PortInfo {
  std::uint32_t port_id = 0;
  std::string name;
  std::uint8_t direction = 0;
  std::uint8_t role = 0;
  std::uint32_t bit_width = 0;
  std::uint32_t word_count = 0;
  std::string protocol;
  bool has_protocol = false;
};

struct Pycstb4Event {
  std::uint64_t cycle = 0;
  std::uint8_t kind = 0;
  std::uint8_t phase = 1;
  std::uint32_t port_id = 0xffffffffu;
  std::uint32_t nwords = 0;
  std::string message;
  bool has_message = false;
  std::vector<std::uint64_t> value_words;
  std::vector<std::uint64_t> mask_words;
};

struct Pycstb4FrameItem {
  std::uint32_t port_id = 0;
  std::uint32_t nwords = 0;
  std::string message;
  bool has_message = false;
  std::vector<std::uint64_t> value_words;
  std::vector<std::uint64_t> mask_words;
};

struct Pycstb4Frame {
  std::uint64_t cycle = 0;
  std::uint8_t kind = 0;
  std::uint8_t phase = 0;
  std::vector<Pycstb4FrameItem> items;
};

struct Pycstb4PeriodicDrive {
  std::uint32_t port_id = 0;
  std::uint64_t start_cycle = 0;
  std::uint64_t end_cycle = 0;
  std::uint64_t period = 1;
  std::uint64_t active_cycles = 0;
  std::uint64_t phase_cycle = 0;
  std::uint32_t active_nwords = 0;
  std::uint32_t default_nwords = 0;
  std::vector<std::uint64_t> active_words;
  std::vector<std::uint64_t> default_words;

  bool activeAt(std::uint64_t cycle) const {
    if (cycle < start_cycle || cycle >= end_cycle || period == 0) return false;
    return ((cycle - phase_cycle) % period) < active_cycles;
  }
};

struct Pycstb4ActorExternalSource {
  std::uint32_t kind = 0;
  std::string path;
  std::uint32_t count = 0;
  std::uint64_t byte_offset = 0;
  std::uint64_t byte_size = 0;
  std::vector<std::uint32_t> payload_ports;
};

struct Pycstb4ActorRecord {
  std::uint16_t kind = 0;
  std::uint16_t policy = 0;
  std::string name;
  std::uint32_t valid_port = 0xffffffffu;
  std::uint32_t ready_port = 0xffffffffu;
  std::vector<std::uint32_t> payload_ports;
  std::uint64_t start_cycle = 0;
  std::uint64_t end_cycle = 0;
  std::uint32_t source_ref = 0xffffffffu;
  std::uint32_t scoreboard_ref = 0xffffffffu;
  std::uint16_t ready_kind = 0;
  std::uint64_t ready_period = 1;
  std::uint64_t ready_active_cycles = 0;
  std::uint64_t ready_phase_cycle = 0;
  std::uint64_t ready_start_cycle = 0;
  std::uint64_t ready_end_cycle = 0;
  std::uint64_t ready_active_value = 1;
  std::uint64_t ready_default_value = 1;
};

struct Pycstb4ScoreboardRecord {
  std::uint16_t kind = 0;
  std::uint16_t flags = 0;
  std::string name;
  std::vector<std::uint32_t> payload_ports;
  std::uint32_t expected_ref = 0xffffffffu;
};

struct Pycstb4ActorPayloadTable {
  std::vector<std::uint32_t> payload_ports;
  std::uint32_t transaction_count = 0;
  std::uint32_t payload_word_count = 0;
  std::uint32_t flags = 0;
  std::vector<std::uint64_t> words;
};

struct Pycstb4InstructionStream {
  std::string name;
  std::string isa;
  std::string encoding;
  std::string source;
  std::uint32_t instruction_width = 0;
  std::uint32_t flags = 0;
  std::uint64_t count = 0;
  std::uint64_t payload_offset = 0;
  std::uint64_t payload_size = 0;
  std::vector<std::uint8_t> payload;
};

struct Pycstb4SeededWorkloadGenerator {
  std::string name;
  std::string generator_id;
  std::string profile;
  std::string constraints_json;
  std::uint64_t seed = 0;
  std::uint64_t count = 0;
  std::uint64_t start_index = 0;
  std::uint64_t flags = 0;
  std::vector<std::uint32_t> output_ports;
};

struct Pycstb4ExternalStreamSource {
  std::string name;
  std::string path;
  std::string format;
  std::string hash;
  std::uint64_t offset = 0;
  std::uint64_t byte_size = 0;
  std::uint64_t chunk_size = 0;
  std::uint64_t flags = 0;
};

struct Pycstb4ScoreboardPolicy {
  std::string name;
  std::string kind;
  std::string target;
  std::string reference;
  std::string signature;
  std::uint64_t sample_period = 0;
  std::uint64_t max_mismatches = 0;
  std::uint64_t flags = 0;
};

struct Pycstb4Schedule {
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint16_t patch = 0;
  std::uint64_t flags = 0;
  std::uint32_t max_words = 0;
  std::uint64_t max_cycle = 0;
  std::uint32_t reset_cycles = 0;
  std::vector<Pycstb4SectionInfo> sections;
  std::vector<std::string> strings;
  std::vector<Pycstb4PortInfo> ports;
  std::vector<Pycstb4Event> events;
  std::vector<Pycstb4Frame> frames;
  std::vector<Pycstb4PeriodicDrive> periodic_drives;
  std::vector<std::uint32_t> actor_port_refs;
  std::vector<Pycstb4ActorExternalSource> actor_external_sources;
  std::vector<Pycstb4ActorRecord> actors;
  std::vector<Pycstb4ScoreboardRecord> scoreboards;
  std::vector<std::uint8_t> actor_payload_blob;
  std::vector<Pycstb4ActorPayloadTable> actor_payload_tables;
  std::vector<Pycstb4InstructionStream> instruction_streams;
  std::vector<Pycstb4SeededWorkloadGenerator> seeded_workload_generators;
  std::vector<Pycstb4ExternalStreamSource> external_stream_sources;
  std::vector<Pycstb4ScoreboardPolicy> scoreboard_policies;
};

inline bool verifyPycstb4ScheduleBasic(const Pycstb4Schedule &schedule, std::string *err = nullptr) {
  bool has_strings = false;
  bool has_ports = false;
  bool has_events = false;
  bool has_frames = false;
  for (const auto &section : schedule.sections) {
    if (!pycstb4SectionSupported(section.kind)) {
      // Unknown sections are tolerated at this prototype stage. Future required
      // section flags should make this stricter.
      continue;
    }
    if (section.kind == kPycstb4SectionStringTable) has_strings = true;
    if (section.kind == kPycstb4SectionPortTable) has_ports = true;
    if (section.kind == kPycstb4SectionEventTable) has_events = true;
    if (section.kind == kPycstb4SectionFrameTable) has_frames = true;
  }
  if (!has_strings || !has_ports || !has_events || !has_frames) {
    if (err != nullptr) *err = "PYCSTB4 missing one or more core sections";
    return false;
  }

  std::vector<std::uint32_t> port_ids;
  port_ids.reserve(schedule.ports.size());
  for (const auto &port : schedule.ports) {
    if (port.bit_width == 0 || port.word_count == 0) {
      if (err != nullptr) {
        *err = "PYCSTB4 port id=" + std::to_string(port.port_id) +
               " has invalid width/word_count";
      }
      return false;
    }
    for (const auto existing : port_ids) {
      if (existing == port.port_id) {
        if (err != nullptr) *err = "PYCSTB4 duplicate port id=" + std::to_string(port.port_id);
        return false;
      }
    }
    port_ids.push_back(port.port_id);
  }
  auto has_port = [&](std::uint32_t port_id) -> bool {
    for (const auto &port : schedule.ports) {
      if (port.port_id == port_id) return true;
    }
    return false;
  };

  for (const auto &event : schedule.events) {
    if (event.port_id != 0xffffffffu && !has_port(event.port_id)) {
      if (err != nullptr) {
        *err = "PYCSTB4 event cycle=" + std::to_string(event.cycle) +
               " references missing port id=" + std::to_string(event.port_id);
      }
      return false;
    }
  }
  for (const auto &frame : schedule.frames) {
    for (const auto &item : frame.items) {
      if (!has_port(item.port_id)) {
        if (err != nullptr) {
          *err = "PYCSTB4 frame cycle=" + std::to_string(frame.cycle) +
                 " references missing port id=" + std::to_string(item.port_id);
        }
        return false;
      }
    }
  }
  for (const auto &pattern : schedule.periodic_drives) {
    if (!has_port(pattern.port_id) || pattern.period == 0 || pattern.active_cycles > pattern.period) {
      if (err != nullptr) {
        *err = "PYCSTB4 pattern port id=" + std::to_string(pattern.port_id) +
               " is invalid period=" + std::to_string(pattern.period) +
               " active_cycles=" + std::to_string(pattern.active_cycles);
      }
      return false;
    }
  }
  for (const auto &actor : schedule.actors) {
    if (!has_port(actor.valid_port) || !has_port(actor.ready_port) || actor.start_cycle > actor.end_cycle) {
      if (err != nullptr) {
        *err = "PYCSTB4 actor '" + actor.name + "' has invalid ports or cycle range";
      }
      return false;
    }
    for (const auto port_id : actor.payload_ports) {
      if (!has_port(port_id)) {
        if (err != nullptr) {
          *err = "PYCSTB4 actor '" + actor.name +
                 "' payload references missing port id=" + std::to_string(port_id);
        }
        return false;
      }
    }
  }
  for (const auto &generator : schedule.seeded_workload_generators) {
    if (generator.generator_id.empty()) {
      if (err != nullptr) *err = "PYCSTB4 seeded workload generator has empty generator_id";
      return false;
    }
    for (const auto port_id : generator.output_ports) {
      if (!has_port(port_id)) {
        if (err != nullptr) {
          *err = "PYCSTB4 seeded workload generator '" + generator.name +
                 "' references missing port id=" + std::to_string(port_id);
        }
        return false;
      }
    }
  }
  return true;
}

namespace detail {

class Pycstb4Reader {
 public:
  explicit Pycstb4Reader(const std::vector<std::uint8_t> &data) : data_(data) {}

  bool seek(std::uint64_t offset, std::string *err) {
    if (offset > data_.size()) {
      fail(err, "seek past end of file");
      return false;
    }
    pos_ = static_cast<std::size_t>(offset);
    return true;
  }

  bool readU8(std::uint8_t *out, std::string *err) {
    if (!need(1, err)) return false;
    *out = data_[pos_++];
    return true;
  }

  bool readU16(std::uint16_t *out, std::string *err) {
    if (!need(2, err)) return false;
    *out = static_cast<std::uint16_t>(data_[pos_]) |
           (static_cast<std::uint16_t>(data_[pos_ + 1]) << 8);
    pos_ += 2;
    return true;
  }

  bool readU32(std::uint32_t *out, std::string *err) {
    if (!need(4, err)) return false;
    *out = static_cast<std::uint32_t>(data_[pos_]) |
           (static_cast<std::uint32_t>(data_[pos_ + 1]) << 8) |
           (static_cast<std::uint32_t>(data_[pos_ + 2]) << 16) |
           (static_cast<std::uint32_t>(data_[pos_ + 3]) << 24);
    pos_ += 4;
    return true;
  }

  bool readU64(std::uint64_t *out, std::string *err) {
    if (!need(8, err)) return false;
    std::uint64_t v = 0;
    for (std::size_t i = 0; i < 8; ++i) {
      v |= static_cast<std::uint64_t>(data_[pos_ + i]) << (8 * i);
    }
    pos_ += 8;
    *out = v;
    return true;
  }

  bool readBytes(std::uint32_t len, std::string *out, std::string *err) {
    if (!need(len, err)) return false;
    out->assign(reinterpret_cast<const char *>(&data_[pos_]), static_cast<std::size_t>(len));
    pos_ += len;
    return true;
  }

  bool readRawBytes(std::uint64_t len, std::vector<std::uint8_t> *out, std::string *err) {
    if (len > static_cast<std::uint64_t>(data_.size() - pos_)) {
      fail(err, "unexpected end of PYCSTB4 file");
      return false;
    }
    out->assign(data_.begin() + static_cast<std::ptrdiff_t>(pos_), data_.begin() + static_cast<std::ptrdiff_t>(pos_ + len));
    pos_ += static_cast<std::size_t>(len);
    return true;
  }

 private:
  bool need(std::size_t n, std::string *err) const {
    if (pos_ + n > data_.size()) {
      fail(err, "unexpected end of PYCSTB4 file");
      return false;
    }
    return true;
  }

  static void fail(std::string *err, const std::string &msg) {
    if (err != nullptr) *err = msg;
  }

  const std::vector<std::uint8_t> &data_;
  std::size_t pos_ = 0;
};

inline bool pycstb4ReadFile(const std::filesystem::path &path, std::vector<std::uint8_t> *out, std::string *err) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    if (err != nullptr) *err = "failed to open PYCSTB4 file: " + path.string();
    return false;
  }
  f.seekg(0, std::ios::end);
  const std::streamoff size = f.tellg();
  if (size < 0) {
    if (err != nullptr) *err = "failed to size PYCSTB4 file: " + path.string();
    return false;
  }
  f.seekg(0, std::ios::beg);
  out->resize(static_cast<std::size_t>(size));
  if (size != 0) f.read(reinterpret_cast<char *>(out->data()), size);
  if (!f) {
    if (err != nullptr) *err = "failed to read PYCSTB4 file: " + path.string();
    return false;
  }
  return true;
}

inline const Pycstb4SectionInfo *findPycstb4Section(const Pycstb4Schedule &schedule, std::uint16_t kind) {
  for (const auto &section : schedule.sections) {
    if (section.kind == kind) return &section;
  }
  return nullptr;
}

inline bool pycstb4StringById(const Pycstb4Schedule &schedule, std::uint32_t sid, std::string *out, std::string *err) {
  if (sid == 0xffffffffu) {
    out->clear();
    return true;
  }
  if (sid >= schedule.strings.size()) {
    if (err != nullptr) *err = "PYCSTB4 string id out of range";
    return false;
  }
  *out = schedule.strings[sid];
  return true;
}

inline bool loadPycstb4Strings(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  std::uint32_t count = 0;
  if (!r->readU32(&count, err)) return false;
  schedule->strings.clear();
  schedule->strings.reserve(count);
  for (std::uint32_t i = 0; i < count; ++i) {
    std::uint32_t len = 0;
    std::string value;
    if (!r->readU32(&len, err)) return false;
    if (!r->readBytes(len, &value, err)) return false;
    schedule->strings.push_back(value);
  }
  return true;
}

inline bool loadPycstb4Ports(Pycstb4Reader *r, std::uint64_t count, Pycstb4Schedule *schedule, std::string *err) {
  schedule->ports.clear();
  schedule->ports.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    Pycstb4PortInfo port;
    std::uint32_t name_sid = 0;
    std::uint32_t protocol_sid = 0;
    std::uint16_t reserved = 0;
    if (!r->readU32(&port.port_id, err)) return false;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU8(&port.direction, err)) return false;
    if (!r->readU8(&port.role, err)) return false;
    if (!r->readU16(&reserved, err)) return false;
    if (!r->readU32(&port.bit_width, err)) return false;
    if (!r->readU32(&port.word_count, err)) return false;
    if (!r->readU32(&protocol_sid, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &port.name, err)) return false;
    if (protocol_sid != 0xffffffffu) {
      port.has_protocol = true;
      if (!pycstb4StringById(*schedule, protocol_sid, &port.protocol, err)) return false;
    }
    schedule->ports.push_back(port);
  }
  return true;
}

inline bool readPycstb4Words(Pycstb4Reader *r, std::uint32_t max_words, std::vector<std::uint64_t> *out, std::string *err) {
  out->clear();
  out->reserve(max_words);
  for (std::uint32_t i = 0; i < max_words; ++i) {
    std::uint64_t word = 0;
    if (!r->readU64(&word, err)) return false;
    out->push_back(word);
  }
  return true;
}

inline bool loadPycstb4Events(Pycstb4Reader *r, std::uint64_t count, Pycstb4Schedule *schedule, std::string *err) {
  schedule->events.clear();
  schedule->events.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    Pycstb4Event event;
    std::uint16_t reserved = 0;
    std::uint32_t message_sid = 0;
    if (!r->readU64(&event.cycle, err)) return false;
    if (!r->readU8(&event.kind, err)) return false;
    if (!r->readU8(&event.phase, err)) return false;
    if (!r->readU16(&reserved, err)) return false;
    if (!r->readU32(&event.port_id, err)) return false;
    if (!r->readU32(&event.nwords, err)) return false;
    if (!r->readU32(&message_sid, err)) return false;
    event.has_message = message_sid != 0xffffffffu;
    if (!pycstb4StringById(*schedule, message_sid, &event.message, err)) return false;
    if (!readPycstb4Words(r, schedule->max_words, &event.value_words, err)) return false;
    if (!readPycstb4Words(r, schedule->max_words, &event.mask_words, err)) return false;
    schedule->events.push_back(event);
  }
  return true;
}

inline bool loadPycstb4Frames(Pycstb4Reader *r, std::uint64_t count, Pycstb4Schedule *schedule, std::string *err) {
  schedule->frames.clear();
  schedule->frames.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    Pycstb4Frame frame;
    std::uint16_t reserved = 0;
    std::uint32_t item_count = 0;
    if (!r->readU64(&frame.cycle, err)) return false;
    if (!r->readU8(&frame.kind, err)) return false;
    if (!r->readU8(&frame.phase, err)) return false;
    if (!r->readU16(&reserved, err)) return false;
    if (!r->readU32(&item_count, err)) return false;
    frame.items.reserve(item_count);
    for (std::uint32_t item_idx = 0; item_idx < item_count; ++item_idx) {
      Pycstb4FrameItem item;
      std::uint32_t message_sid = 0;
      std::uint32_t reserved_item = 0;
      if (!r->readU32(&item.port_id, err)) return false;
      if (!r->readU32(&item.nwords, err)) return false;
      if (!r->readU32(&message_sid, err)) return false;
      if (!r->readU32(&reserved_item, err)) return false;
      item.has_message = message_sid != 0xffffffffu;
      if (!pycstb4StringById(*schedule, message_sid, &item.message, err)) return false;
      if (!readPycstb4Words(r, schedule->max_words, &item.value_words, err)) return false;
      if (!readPycstb4Words(r, schedule->max_words, &item.mask_words, err)) return false;
      frame.items.push_back(item);
    }
    schedule->frames.push_back(frame);
  }
  return true;
}

inline bool loadPycstb4Patterns(Pycstb4Reader *r, std::uint64_t count, Pycstb4Schedule *schedule, std::string *err) {
  schedule->periodic_drives.clear();
  schedule->periodic_drives.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t i = 0; i < count; ++i) {
    std::uint16_t kind = 0;
    std::uint16_t flags = 0;
    Pycstb4PeriodicDrive pattern;
    if (!r->readU16(&kind, err)) return false;
    if (!r->readU16(&flags, err)) return false;
    if (!r->readU32(&pattern.port_id, err)) return false;
    if (!r->readU64(&pattern.start_cycle, err)) return false;
    if (!r->readU64(&pattern.end_cycle, err)) return false;
    if (!r->readU64(&pattern.period, err)) return false;
    if (!r->readU64(&pattern.active_cycles, err)) return false;
    if (!r->readU64(&pattern.phase_cycle, err)) return false;
    if (!r->readU32(&pattern.active_nwords, err)) return false;
    if (!r->readU32(&pattern.default_nwords, err)) return false;
    if (kind != 1) {
      if (err != nullptr) *err = "unsupported PYCSTB4 pattern kind";
      return false;
    }
    if (!readPycstb4Words(r, schedule->max_words, &pattern.active_words, err)) return false;
    if (!readPycstb4Words(r, schedule->max_words, &pattern.default_words, err)) return false;
    schedule->periodic_drives.push_back(pattern);
  }
  return true;
}

inline bool pycstb4SlicePortRefs(
    const std::vector<std::uint32_t> &refs,
    std::uint32_t first,
    std::uint32_t count,
    std::vector<std::uint32_t> *out,
    std::string *err) {
  if (static_cast<std::uint64_t>(first) + static_cast<std::uint64_t>(count) > refs.size()) {
    if (err != nullptr) *err = "PYCSTB4 actor bundle port-ref slice out of range";
    return false;
  }
  out->assign(refs.begin() + first, refs.begin() + first + count);
  return true;
}

inline bool loadPycstb4ActorBundle(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'A' && magic[1] == 'C' && magic[2] == 'T' && magic[3] == 'R')) {
    if (err != nullptr) *err = "invalid PYCSTB4 actor bundle magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t external_count = 0;
  std::uint32_t actor_count = 0;
  std::uint32_t scoreboard_count = 0;
  std::uint32_t port_ref_count = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&external_count, err)) return false;
  if (!r->readU32(&actor_count, err)) return false;
  if (!r->readU32(&scoreboard_count, err)) return false;
  if (!r->readU32(&port_ref_count, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 actor bundle version";
    return false;
  }

  schedule->actor_port_refs.clear();
  schedule->actor_port_refs.reserve(port_ref_count);
  for (std::uint32_t i = 0; i < port_ref_count; ++i) {
    std::uint32_t port_id = 0;
    if (!r->readU32(&port_id, err)) return false;
    schedule->actor_port_refs.push_back(port_id);
  }

  schedule->actor_external_sources.clear();
  schedule->actor_external_sources.reserve(external_count);
  for (std::uint32_t i = 0; i < external_count; ++i) {
    Pycstb4ActorExternalSource source;
    std::uint32_t path_sid = 0;
    std::uint64_t reserved = 0;
    std::uint32_t first_payload = 0;
    std::uint32_t payload_count = 0;
    if (!r->readU32(&source.kind, err)) return false;
    if (!r->readU32(&path_sid, err)) return false;
    if (!r->readU32(&source.count, err)) return false;
    if (!r->readU64(&source.byte_offset, err)) return false;
    if (!r->readU64(&source.byte_size, err)) return false;
    if (!r->readU64(&reserved, err)) return false;
    if (!r->readU32(&first_payload, err)) return false;
    if (!r->readU32(&payload_count, err)) return false;
    if (!pycstb4StringById(*schedule, path_sid, &source.path, err)) return false;
    if (!pycstb4SlicePortRefs(schedule->actor_port_refs, first_payload, payload_count, &source.payload_ports, err)) return false;
    schedule->actor_external_sources.push_back(source);
  }

  schedule->actors.clear();
  schedule->actors.reserve(actor_count);
  for (std::uint32_t i = 0; i < actor_count; ++i) {
    Pycstb4ActorRecord actor;
    std::uint32_t name_sid = 0;
    std::uint32_t first_payload = 0;
    std::uint32_t payload_count = 0;
    std::uint16_t reserved = 0;
    if (!r->readU16(&actor.kind, err)) return false;
    if (!r->readU16(&actor.policy, err)) return false;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&actor.valid_port, err)) return false;
    if (!r->readU32(&actor.ready_port, err)) return false;
    if (!r->readU32(&first_payload, err)) return false;
    if (!r->readU32(&payload_count, err)) return false;
    if (!r->readU64(&actor.start_cycle, err)) return false;
    if (!r->readU64(&actor.end_cycle, err)) return false;
    if (!r->readU32(&actor.source_ref, err)) return false;
    if (!r->readU32(&actor.scoreboard_ref, err)) return false;
    if (!r->readU16(&actor.ready_kind, err)) return false;
    if (!r->readU16(&reserved, err)) return false;
    if (!r->readU64(&actor.ready_period, err)) return false;
    if (!r->readU64(&actor.ready_active_cycles, err)) return false;
    if (!r->readU64(&actor.ready_phase_cycle, err)) return false;
    if (!r->readU64(&actor.ready_start_cycle, err)) return false;
    if (!r->readU64(&actor.ready_end_cycle, err)) return false;
    if (!r->readU64(&actor.ready_active_value, err)) return false;
    if (!r->readU64(&actor.ready_default_value, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &actor.name, err)) return false;
    if (!pycstb4SlicePortRefs(schedule->actor_port_refs, first_payload, payload_count, &actor.payload_ports, err)) return false;
    schedule->actors.push_back(actor);
  }

  schedule->scoreboards.clear();
  schedule->scoreboards.reserve(scoreboard_count);
  for (std::uint32_t i = 0; i < scoreboard_count; ++i) {
    Pycstb4ScoreboardRecord scoreboard;
    std::uint32_t name_sid = 0;
    std::uint32_t first_payload = 0;
    std::uint32_t payload_count = 0;
    if (!r->readU16(&scoreboard.kind, err)) return false;
    if (!r->readU16(&scoreboard.flags, err)) return false;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&first_payload, err)) return false;
    if (!r->readU32(&payload_count, err)) return false;
    if (!r->readU32(&scoreboard.expected_ref, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &scoreboard.name, err)) return false;
    if (!pycstb4SlicePortRefs(schedule->actor_port_refs, first_payload, payload_count, &scoreboard.payload_ports, err)) return false;
    schedule->scoreboards.push_back(scoreboard);
  }
  return true;
}

inline bool loadPycstb4ActorPayloadTables(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'A' && magic[1] == 'T' && magic[2] == 'X' && magic[3] == 'N')) {
    if (err != nullptr) *err = "invalid PYCSTB4 actor payload table magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t table_count = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&table_count, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 actor payload table version";
    return false;
  }
  schedule->actor_payload_tables.clear();
  schedule->actor_payload_tables.reserve(table_count);
  for (std::uint32_t table_idx = 0; table_idx < table_count; ++table_idx) {
    Pycstb4ActorPayloadTable table;
    std::uint32_t payload_count = 0;
    if (!r->readU32(&payload_count, err)) return false;
    if (!r->readU32(&table.transaction_count, err)) return false;
    if (!r->readU32(&table.payload_word_count, err)) return false;
    if (!r->readU32(&table.flags, err)) return false;
    table.payload_ports.reserve(payload_count);
    for (std::uint32_t i = 0; i < payload_count; ++i) {
      std::uint32_t port_id = 0;
      if (!r->readU32(&port_id, err)) return false;
      table.payload_ports.push_back(port_id);
    }
    const std::uint64_t word_count =
        static_cast<std::uint64_t>(table.transaction_count) *
        static_cast<std::uint64_t>(payload_count) *
        static_cast<std::uint64_t>(table.payload_word_count);
    table.words.reserve(static_cast<std::size_t>(word_count));
    for (std::uint64_t i = 0; i < word_count; ++i) {
      std::uint64_t word = 0;
      if (!r->readU64(&word, err)) return false;
      table.words.push_back(word);
    }
    schedule->actor_payload_tables.push_back(std::move(table));
  }
  return true;
}

inline bool loadPycstb4SeededWorkloadGenerators(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'W' && magic[1] == 'G' && magic[2] == 'E' && magic[3] == 'N')) {
    if (err != nullptr) *err = "invalid PYCSTB4 seeded workload generator magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t generator_count = 0;
  std::uint32_t port_ref_count = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&generator_count, err)) return false;
  if (!r->readU32(&port_ref_count, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 seeded workload generator version";
    return false;
  }
  std::vector<std::uint32_t> port_refs;
  port_refs.reserve(port_ref_count);
  for (std::uint32_t i = 0; i < port_ref_count; ++i) {
    std::uint32_t port_id = 0;
    if (!r->readU32(&port_id, err)) return false;
    port_refs.push_back(port_id);
  }
  schedule->seeded_workload_generators.clear();
  schedule->seeded_workload_generators.reserve(generator_count);
  for (std::uint32_t i = 0; i < generator_count; ++i) {
    std::uint32_t name_sid = 0xffffffffu;
    std::uint32_t generator_sid = 0xffffffffu;
    std::uint32_t profile_sid = 0xffffffffu;
    std::uint32_t constraints_sid = 0xffffffffu;
    std::uint32_t first_output_ref = 0;
    std::uint32_t output_port_count = 0;
    Pycstb4SeededWorkloadGenerator generator;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&generator_sid, err)) return false;
    if (!r->readU32(&profile_sid, err)) return false;
    if (!r->readU32(&constraints_sid, err)) return false;
    if (!r->readU64(&generator.seed, err)) return false;
    if (!r->readU64(&generator.count, err)) return false;
    if (!r->readU64(&generator.start_index, err)) return false;
    if (!r->readU64(&generator.flags, err)) return false;
    if (!r->readU32(&first_output_ref, err)) return false;
    if (!r->readU32(&output_port_count, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &generator.name, err)) return false;
    if (!pycstb4StringById(*schedule, generator_sid, &generator.generator_id, err)) return false;
    if (!pycstb4StringById(*schedule, profile_sid, &generator.profile, err)) return false;
    if (!pycstb4StringById(*schedule, constraints_sid, &generator.constraints_json, err)) return false;
    if (static_cast<std::uint64_t>(first_output_ref) + output_port_count > port_refs.size()) {
      if (err != nullptr) *err = "PYCSTB4 seeded workload generator port ref out of range";
      return false;
    }
    generator.output_ports.reserve(output_port_count);
    for (std::uint32_t ref = 0; ref < output_port_count; ++ref) {
      generator.output_ports.push_back(port_refs[first_output_ref + ref]);
    }
    schedule->seeded_workload_generators.push_back(std::move(generator));
  }
  return true;
}

inline bool loadPycstb4InstructionStreams(
    Pycstb4Reader *r,
    std::uint64_t section_offset,
    Pycstb4Schedule *schedule,
    std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'I' && magic[1] == 'N' && magic[2] == 'S' && magic[3] == 'T')) {
    if (err != nullptr) *err = "invalid PYCSTB4 instruction stream magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t stream_count = 0;
  std::uint32_t reserved = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&stream_count, err)) return false;
  if (!r->readU32(&reserved, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 instruction stream version";
    return false;
  }
  schedule->instruction_streams.clear();
  schedule->instruction_streams.reserve(stream_count);
  for (std::uint32_t i = 0; i < stream_count; ++i) {
    Pycstb4InstructionStream stream;
    std::uint32_t name_sid = 0xffffffffu;
    std::uint32_t isa_sid = 0xffffffffu;
    std::uint32_t encoding_sid = 0xffffffffu;
    std::uint32_t source_sid = 0xffffffffu;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&isa_sid, err)) return false;
    if (!r->readU32(&encoding_sid, err)) return false;
    if (!r->readU32(&source_sid, err)) return false;
    if (!r->readU32(&stream.instruction_width, err)) return false;
    if (!r->readU32(&stream.flags, err)) return false;
    if (!r->readU64(&stream.count, err)) return false;
    if (!r->readU64(&stream.payload_offset, err)) return false;
    if (!r->readU64(&stream.payload_size, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &stream.name, err)) return false;
    if (!pycstb4StringById(*schedule, isa_sid, &stream.isa, err)) return false;
    if (!pycstb4StringById(*schedule, encoding_sid, &stream.encoding, err)) return false;
    if (!pycstb4StringById(*schedule, source_sid, &stream.source, err)) return false;
    schedule->instruction_streams.push_back(std::move(stream));
  }
  for (auto &stream : schedule->instruction_streams) {
    if (stream.payload_size == 0) {
      stream.payload.clear();
      continue;
    }
    if (!r->seek(section_offset + stream.payload_offset, err)) return false;
    if (!r->readRawBytes(stream.payload_size, &stream.payload, err)) return false;
  }
  return true;
}

inline bool loadPycstb4ExternalStreamSources(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'X' && magic[1] == 'S' && magic[2] == 'T' && magic[3] == 'R')) {
    if (err != nullptr) *err = "invalid PYCSTB4 external stream source magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t source_count = 0;
  std::uint32_t reserved = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&source_count, err)) return false;
  if (!r->readU32(&reserved, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 external stream source version";
    return false;
  }
  schedule->external_stream_sources.clear();
  schedule->external_stream_sources.reserve(source_count);
  for (std::uint32_t i = 0; i < source_count; ++i) {
    Pycstb4ExternalStreamSource source;
    std::uint32_t name_sid = 0xffffffffu;
    std::uint32_t path_sid = 0xffffffffu;
    std::uint32_t format_sid = 0xffffffffu;
    std::uint32_t hash_sid = 0xffffffffu;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&path_sid, err)) return false;
    if (!r->readU32(&format_sid, err)) return false;
    if (!r->readU32(&hash_sid, err)) return false;
    if (!r->readU64(&source.offset, err)) return false;
    if (!r->readU64(&source.byte_size, err)) return false;
    if (!r->readU64(&source.chunk_size, err)) return false;
    if (!r->readU64(&source.flags, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &source.name, err)) return false;
    if (!pycstb4StringById(*schedule, path_sid, &source.path, err)) return false;
    if (!pycstb4StringById(*schedule, format_sid, &source.format, err)) return false;
    if (!pycstb4StringById(*schedule, hash_sid, &source.hash, err)) return false;
    schedule->external_stream_sources.push_back(std::move(source));
  }
  return true;
}

inline bool loadPycstb4ScoreboardPolicies(Pycstb4Reader *r, Pycstb4Schedule *schedule, std::string *err) {
  char magic[4] = {};
  for (char &c : magic) {
    std::uint8_t byte = 0;
    if (!r->readU8(&byte, err)) return false;
    c = static_cast<char>(byte);
  }
  if (!(magic[0] == 'S' && magic[1] == 'C' && magic[2] == 'B' && magic[3] == 'P')) {
    if (err != nullptr) *err = "invalid PYCSTB4 scoreboard policy magic";
    return false;
  }
  std::uint16_t major = 0;
  std::uint16_t minor = 0;
  std::uint32_t policy_count = 0;
  std::uint32_t reserved = 0;
  if (!r->readU16(&major, err)) return false;
  if (!r->readU16(&minor, err)) return false;
  if (!r->readU32(&policy_count, err)) return false;
  if (!r->readU32(&reserved, err)) return false;
  if (major != 0 || minor != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 scoreboard policy version";
    return false;
  }
  schedule->scoreboard_policies.clear();
  schedule->scoreboard_policies.reserve(policy_count);
  for (std::uint32_t i = 0; i < policy_count; ++i) {
    Pycstb4ScoreboardPolicy policy;
    std::uint32_t name_sid = 0xffffffffu;
    std::uint32_t kind_sid = 0xffffffffu;
    std::uint32_t target_sid = 0xffffffffu;
    std::uint32_t reference_sid = 0xffffffffu;
    std::uint32_t signature_sid = 0xffffffffu;
    if (!r->readU32(&name_sid, err)) return false;
    if (!r->readU32(&kind_sid, err)) return false;
    if (!r->readU32(&target_sid, err)) return false;
    if (!r->readU32(&reference_sid, err)) return false;
    if (!r->readU32(&signature_sid, err)) return false;
    if (!r->readU64(&policy.sample_period, err)) return false;
    if (!r->readU64(&policy.max_mismatches, err)) return false;
    if (!r->readU64(&policy.flags, err)) return false;
    if (!pycstb4StringById(*schedule, name_sid, &policy.name, err)) return false;
    if (!pycstb4StringById(*schedule, kind_sid, &policy.kind, err)) return false;
    if (!pycstb4StringById(*schedule, target_sid, &policy.target, err)) return false;
    if (!pycstb4StringById(*schedule, reference_sid, &policy.reference, err)) return false;
    if (!pycstb4StringById(*schedule, signature_sid, &policy.signature, err)) return false;
    schedule->scoreboard_policies.push_back(std::move(policy));
  }
  return true;
}

}  // namespace detail

inline bool loadPycstb4Schedule(const std::filesystem::path &path, Pycstb4Schedule *schedule, std::string *err = nullptr) {
  if (schedule == nullptr) {
    if (err != nullptr) *err = "null Pycstb4Schedule output";
    return false;
  }
  std::vector<std::uint8_t> data;
  if (!detail::pycstb4ReadFile(path, &data, err)) return false;
  detail::Pycstb4Reader r(data);

  const char expected_magic[8] = {'P', 'Y', 'C', 'S', 'T', 'B', '4', '\n'};
  for (char c : expected_magic) {
    std::uint8_t got = 0;
    if (!r.readU8(&got, err)) return false;
    if (got != static_cast<std::uint8_t>(c)) {
      if (err != nullptr) *err = "invalid PYCSTB4 magic";
      return false;
    }
  }

  std::uint8_t endian = 0;
  std::uint16_t header_size = 0;
  std::uint32_t section_count = 0;
  std::uint32_t reserved = 0;
  if (!r.readU8(&endian, err)) return false;
  if (!r.readU16(&header_size, err)) return false;
  if (!r.readU16(&schedule->major, err)) return false;
  if (!r.readU16(&schedule->minor, err)) return false;
  if (!r.readU16(&schedule->patch, err)) return false;
  if (!r.readU64(&schedule->flags, err)) return false;
  if (!r.readU32(&section_count, err)) return false;
  if (!r.readU32(&schedule->max_words, err)) return false;
  if (!r.readU64(&schedule->max_cycle, err)) return false;
  if (!r.readU32(&schedule->reset_cycles, err)) return false;
  if (!r.readU32(&reserved, err)) return false;
  if (endian != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 endian marker";
    return false;
  }
  if (schedule->major != 1) {
    if (err != nullptr) *err = "unsupported PYCSTB4 major version";
    return false;
  }

  schedule->sections.clear();
  schedule->sections.reserve(section_count);
  if (!r.seek(header_size, err)) return false;
  for (std::uint32_t i = 0; i < section_count; ++i) {
    Pycstb4SectionInfo section;
    std::uint32_t reserved_section = 0;
    if (!r.readU16(&section.kind, err)) return false;
    if (!r.readU16(&section.flags, err)) return false;
    if (!r.readU32(&reserved_section, err)) return false;
    if (!r.readU64(&section.offset, err)) return false;
    if (!r.readU64(&section.size, err)) return false;
    if (!r.readU64(&section.count, err)) return false;
    schedule->sections.push_back(section);
  }

  const Pycstb4SectionInfo *strings = detail::findPycstb4Section(*schedule, 1);
  const Pycstb4SectionInfo *ports = detail::findPycstb4Section(*schedule, 2);
  const Pycstb4SectionInfo *events = detail::findPycstb4Section(*schedule, 3);
  const Pycstb4SectionInfo *frames = detail::findPycstb4Section(*schedule, 4);
  const Pycstb4SectionInfo *patterns = detail::findPycstb4Section(*schedule, 5);
  const Pycstb4SectionInfo *actor_bundle = detail::findPycstb4Section(*schedule, 16);
  const Pycstb4SectionInfo *actor_payload_blob = detail::findPycstb4Section(*schedule, 17);
  const Pycstb4SectionInfo *actor_payload_table = detail::findPycstb4Section(*schedule, 18);
  const Pycstb4SectionInfo *instruction_streams = detail::findPycstb4Section(*schedule, 19);
  const Pycstb4SectionInfo *seeded_generators = detail::findPycstb4Section(*schedule, 20);
  const Pycstb4SectionInfo *external_stream_sources = detail::findPycstb4Section(*schedule, 21);
  const Pycstb4SectionInfo *scoreboard_policies = detail::findPycstb4Section(*schedule, 22);
  if (strings == nullptr || ports == nullptr || events == nullptr || frames == nullptr) {
    if (err != nullptr) *err = "PYCSTB4 missing required section";
    return false;
  }

  if (!r.seek(strings->offset, err)) return false;
  if (!detail::loadPycstb4Strings(&r, schedule, err)) return false;
  if (!r.seek(ports->offset, err)) return false;
  if (!detail::loadPycstb4Ports(&r, ports->count, schedule, err)) return false;
  if (!r.seek(events->offset, err)) return false;
  if (!detail::loadPycstb4Events(&r, events->count, schedule, err)) return false;
  if (!r.seek(frames->offset, err)) return false;
  if (!detail::loadPycstb4Frames(&r, frames->count, schedule, err)) return false;
  if (patterns != nullptr) {
    if (!r.seek(patterns->offset, err)) return false;
    if (!detail::loadPycstb4Patterns(&r, patterns->count, schedule, err)) return false;
  } else {
    schedule->periodic_drives.clear();
  }
  if (actor_bundle != nullptr) {
    if (!r.seek(actor_bundle->offset, err)) return false;
    if (!detail::loadPycstb4ActorBundle(&r, schedule, err)) return false;
  } else {
    schedule->actor_port_refs.clear();
    schedule->actor_external_sources.clear();
    schedule->actors.clear();
    schedule->scoreboards.clear();
  }
  if (actor_payload_blob != nullptr) {
    if (!r.seek(actor_payload_blob->offset, err)) return false;
    if (!r.readRawBytes(actor_payload_blob->size, &schedule->actor_payload_blob, err)) return false;
  } else {
    schedule->actor_payload_blob.clear();
  }
  if (actor_payload_table != nullptr) {
    if (!r.seek(actor_payload_table->offset, err)) return false;
    if (!detail::loadPycstb4ActorPayloadTables(&r, schedule, err)) return false;
  } else {
    schedule->actor_payload_tables.clear();
  }
  if (instruction_streams != nullptr) {
    if (!r.seek(instruction_streams->offset, err)) return false;
    if (!detail::loadPycstb4InstructionStreams(&r, instruction_streams->offset, schedule, err)) return false;
  } else {
    schedule->instruction_streams.clear();
  }
  if (seeded_generators != nullptr) {
    if (!r.seek(seeded_generators->offset, err)) return false;
    if (!detail::loadPycstb4SeededWorkloadGenerators(&r, schedule, err)) return false;
  } else {
    schedule->seeded_workload_generators.clear();
  }
  if (external_stream_sources != nullptr) {
    if (!r.seek(external_stream_sources->offset, err)) return false;
    if (!detail::loadPycstb4ExternalStreamSources(&r, schedule, err)) return false;
  } else {
    schedule->external_stream_sources.clear();
  }
  if (scoreboard_policies != nullptr) {
    if (!r.seek(scoreboard_policies->offset, err)) return false;
    if (!detail::loadPycstb4ScoreboardPolicies(&r, schedule, err)) return false;
  } else {
    schedule->scoreboard_policies.clear();
  }
  return verifyPycstb4ScheduleBasic(*schedule, err);
}

template <std::size_t MaxWords, std::size_t MaxDrivePorts>
inline bool convertPycstb4ToRuntimeLoopSchedule(
    const Pycstb4Schedule &src,
    const std::array<std::uint32_t, MaxDrivePorts> &drive_port_ids,
    RuntimeLoopSchedule<MaxWords, MaxDrivePorts> *dst,
    std::string *err = nullptr) {
  if (dst == nullptr) {
    if (err != nullptr) *err = "null RuntimeLoopSchedule output";
    return false;
  }
  if (src.max_words > MaxWords) {
    if (err != nullptr) *err = "PYCSTB4 max_words exceeds RuntimeLoopSchedule MaxWords";
    return false;
  }
  dst->drive_frames.clear();
  dst->pre_expect_events.clear();
  dst->post_expect_events.clear();

  auto driveSlotForPort = [&](std::uint32_t port_id, std::uint32_t *slot_out) -> bool {
    for (std::uint32_t slot = 0; slot < MaxDrivePorts; ++slot) {
      if (drive_port_ids[slot] == port_id) {
        *slot_out = slot;
        return true;
      }
    }
    return false;
  };

  for (const auto &frame : src.frames) {
    if (frame.kind != 0) {
      if (err != nullptr) *err = "PYCSTB4 converter only supports drive_frame records";
      return false;
    }
    RuntimeLoopDriveFrame<MaxWords, MaxDrivePorts> out_frame{};
    out_frame.cycle = frame.cycle;
    for (const auto &item : frame.items) {
      std::uint32_t slot = 0;
      if (!driveSlotForPort(item.port_id, &slot)) {
        if (err != nullptr) *err = "PYCSTB4 frame references a port not present in drive_port_ids";
        return false;
      }
      out_frame.port_mask[slot / 64u] |= (1ull << (slot % 64u));
      const std::size_t n = item.value_words.size() < MaxWords ? item.value_words.size() : MaxWords;
      for (std::size_t word_idx = 0; word_idx < n; ++word_idx) {
        out_frame.words[slot][word_idx] = item.value_words[word_idx];
      }
    }
    dst->drive_frames.push_back(out_frame);
  }

  for (const auto &event : src.events) {
    if (event.kind != 1) {
      if (err != nullptr) *err = "PYCSTB4 converter only supports expect event records";
      return false;
    }
    RuntimeLoopEvent<MaxWords> out_event{};
    out_event.cycle = event.cycle;
    out_event.port_id = event.port_id;
    out_event.nwords = event.nwords;
    out_event.msg = event.message;
    const std::size_t n = event.value_words.size() < MaxWords ? event.value_words.size() : MaxWords;
    for (std::size_t word_idx = 0; word_idx < n; ++word_idx) {
      out_event.words[word_idx] = event.value_words[word_idx];
    }
    if (event.phase == 0) {
      dst->pre_expect_events.push_back(out_event);
    } else {
      dst->post_expect_events.push_back(out_event);
    }
  }
  return true;
}

}  // namespace pyc::cpp
