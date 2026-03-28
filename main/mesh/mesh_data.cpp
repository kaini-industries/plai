/**
 * @file mesh_data.cpp
 * @author d4rkmen
 * @brief Shared data structures for Meshtastic UI widgets with file-based storage
 * @version 3.0
 * @date 2025-01-03
 *
 * @copyright Copyright (c) 2025
 *
 */

#include "mesh_data.h"
#include "common_define.h"
#include "esp_log.h"
#include <algorithm>
#include <map>
#include <sys/stat.h>
#include <dirent.h>
#include <stdio.h>
#include <string.h>

static const char* TAG = "MESH_DATA";

namespace Mesh
{

    //--------------------------------------------------------------------------
    // Initialization
    //--------------------------------------------------------------------------

    bool MeshDataStore::init()
    {
        if (_initialized)
        {
            return true;
        }

        ESP_LOGI(TAG, "Initializing message store...");

        if (!createDirectories())
        {
            ESP_LOGW(TAG, "Failed to create message directories");
        }

        if (!loadIndex())
        {
            rebuildIndex();
        }

        _initialized = true;
        ESP_LOGI(TAG, "Message store initialized with %d conversations", _message_index.size());
        return true;
    }

    bool MeshDataStore::createDirectories()
    {
        struct stat st;

        const char* parent_dir = "/sdcard/meshtastic";
        if (stat(parent_dir, &st) != 0)
        {
            ESP_LOGD(TAG, "Creating directory: %s", parent_dir);
            if (mkdir(parent_dir, 0755) != 0)
            {
                ESP_LOGE(TAG, "Failed to create directory: %s", parent_dir);
                return false;
            }
        }

        if (stat(MESSAGES_DIR, &st) != 0)
        {
            ESP_LOGD(TAG, "Creating directory: %s", MESSAGES_DIR);
            if (mkdir(MESSAGES_DIR, 0755) != 0)
            {
                ESP_LOGE(TAG, "Failed to create directory: %s", MESSAGES_DIR);
                return false;
            }
        }

        if (stat(TRACEROUTE_DIR, &st) != 0)
        {
            ESP_LOGD(TAG, "Creating directory: %s", TRACEROUTE_DIR);
            if (mkdir(TRACEROUTE_DIR, 0755) != 0)
            {
                ESP_LOGE(TAG, "Failed to create directory: %s", TRACEROUTE_DIR);
                return false;
            }
        }

        return true;
    }

    //--------------------------------------------------------------------------
    // File Path Helpers
    //--------------------------------------------------------------------------

    std::string MeshDataStore::getDMFilePath(uint32_t node_id) const
    {
        char path[64];
        snprintf(path, sizeof(path), "%s/%08lx.msg", MESSAGES_DIR, (unsigned long)node_id);
        return std::string(path);
    }

    std::string MeshDataStore::getChannelFilePath(uint8_t channel) const
    {
        char path[64];
        snprintf(path, sizeof(path), "%s/ch_%d.msg", MESSAGES_DIR, channel);
        return std::string(path);
    }

    std::string MeshDataStore::getTraceRouteFilePath(uint32_t node_id) const
    {
        char path[80];
        snprintf(path, sizeof(path), "%s/%08lx.trc", TRACEROUTE_DIR, (unsigned long)node_id);
        return std::string(path);
    }

    //--------------------------------------------------------------------------
    // Message record conversion
    //--------------------------------------------------------------------------

    static void msgToRecord(const TextMessage& msg, TextMessageRecord& rec)
    {
        memset(&rec, 0, sizeof(rec));
        rec.id = msg.id;
        rec.from = msg.from;
        rec.to = msg.to;
        rec.timestamp = msg.timestamp;
        rec.status = static_cast<uint8_t>(msg.status) | (msg.read ? 0x80 : 0);
        rec.error_code = msg.error_code;
        size_t len = std::min(msg.text.size(), (size_t)MSG_TEXT_MAX);
        rec.text_len = (uint8_t)len;
        memcpy(rec.text, msg.text.c_str(), len);
    }

    static void recordToMsg(const TextMessageRecord& rec, TextMessage& msg, bool is_direct, uint8_t channel)
    {
        msg.id = rec.id;
        msg.from = rec.from;
        msg.to = rec.to;
        msg.timestamp = rec.timestamp;
        msg.channel = channel;
        msg.is_direct = is_direct;
        msg.read = (rec.status & 0x80) != 0;
        msg.status = static_cast<TextMessage::Status>(rec.status & 0x7F);
        msg.error_code = rec.error_code;
        uint8_t len = std::min(rec.text_len, (uint8_t)MSG_TEXT_MAX);
        msg.text = std::string(rec.text, len);
    }

    static inline long msgRecordOffset(uint32_t index)
    {
        return (long)MSG_FILE_HEADER_SIZE + (long)index * (long)sizeof(TextMessageRecord);
    }

    //--------------------------------------------------------------------------
    // Message file helpers
    //--------------------------------------------------------------------------

    static FILE* openMessageFile(const char* path, uint32_t& out_count, const char* mode = "rb")
    {
        out_count = 0;
        FILE* file = fopen(path, mode);
        if (!file)
            return nullptr;

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&count, 4, 1, file) != 1 ||
            magic != MSG_FILE_MAGIC || version != MSG_FILE_VERSION)
        {
            fclose(file);
            return nullptr;
        }
        out_count = count;
        return file;
    }

    static bool readRecordAt(FILE* file, uint32_t index, TextMessageRecord& rec)
    {
        if (fseek(file, msgRecordOffset(index), SEEK_SET) != 0)
            return false;
        return fread(&rec, sizeof(rec), 1, file) == 1;
    }

    //--------------------------------------------------------------------------
    // Message File I/O (fixed-size TextMessageRecord, 256 bytes each)
    //--------------------------------------------------------------------------

    bool MeshDataStore::appendMessageToFile(const std::string& path, const TextMessage& msg)
    {
        TextMessageRecord rec;
        msgToRecord(msg, rec);

        struct stat st;
        bool file_exists = (stat(path.c_str(), &st) == 0);

        if (!file_exists)
        {
            FILE* file = fopen(path.c_str(), "wb");
            if (!file)
            {
                ESP_LOGE(TAG, "Failed to create %s", path.c_str());
                return false;
            }

            uint32_t magic = MSG_FILE_MAGIC;
            uint32_t version = MSG_FILE_VERSION;
            uint32_t count = 1;
            fwrite(&magic, 4, 1, file);
            fwrite(&version, 4, 1, file);
            fwrite(&count, 4, 1, file);
            fwrite(&rec, sizeof(rec), 1, file);
            fclose(file);

            ESP_LOGD(TAG, "Created %s with 1 message", path.c_str());
            return true;
        }

        FILE* file = fopen(path.c_str(), "r+b");
        if (!file)
        {
            ESP_LOGE(TAG, "Failed to open %s for append", path.c_str());
            return false;
        }

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&count, 4, 1, file) != 1 ||
            magic != MSG_FILE_MAGIC || version != MSG_FILE_VERSION)
        {
            fclose(file);
            ESP_LOGW(TAG, "Corrupt message file, recreating: %s", path.c_str());
            file = fopen(path.c_str(), "wb");
            if (!file)
                return false;

            magic = MSG_FILE_MAGIC;
            version = MSG_FILE_VERSION;
            count = 1;
            fwrite(&magic, 4, 1, file);
            fwrite(&version, 4, 1, file);
            fwrite(&count, 4, 1, file);
            fwrite(&rec, sizeof(rec), 1, file);
            fclose(file);
            return true;
        }

        fseek(file, msgRecordOffset(count), SEEK_SET);
        fwrite(&rec, sizeof(rec), 1, file);

        count++;
        fseek(file, 8, SEEK_SET);
        fwrite(&count, 4, 1, file);
        fclose(file);

        ESP_LOGD(TAG, "Appended message to %s (total: %lu)", path.c_str(), (unsigned long)count);
        return true;
    }

    //--------------------------------------------------------------------------
    // Message status update (in-place, iterates from newest)
    //--------------------------------------------------------------------------

    bool MeshDataStore::updateMessageStatus(
        uint32_t packet_id, uint32_t node_id, TextMessage::Status new_status, uint8_t error_code, uint8_t channel)
    {
        std::string path = (node_id == 0xFFFFFFFF) ? getChannelFilePath(channel) : getDMFilePath(node_id);

        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count, "r+b");
        if (!file || count == 0)
        {
            if (file)
                fclose(file);
            ESP_LOGW(TAG, "Message 0x%08lX not found in %s", (unsigned long)packet_id, path.c_str());
            return false;
        }

        bool found = false;
        for (int32_t i = (int32_t)count - 1; i >= 0; i--)
        {
            long offset = msgRecordOffset((uint32_t)i);
            fseek(file, offset, SEEK_SET);

            uint32_t msg_id = 0;
            if (fread(&msg_id, 4, 1, file) != 1)
                break;

            if (msg_id == packet_id)
            {
                long status_off = offset + offsetof(TextMessageRecord, status);
                fseek(file, status_off, SEEK_SET);
                uint8_t st = static_cast<uint8_t>(new_status);
                fwrite(&st, 1, 1, file);
                fwrite(&error_code, 1, 1, file);
                fflush(file);
                found = true;
                ESP_LOGD(TAG,
                         "Updated message 0x%08lX status=%d err=%d in %s",
                         (unsigned long)packet_id,
                         (int)new_status,
                         (int)error_code,
                         path.c_str());
                break;
            }
        }

        fclose(file);

        if (found)
            _change_counter++;
        else
            ESP_LOGW(TAG, "Message 0x%08lX not found in %s", (unsigned long)packet_id, path.c_str());

        return found;
    }

    //--------------------------------------------------------------------------
    // Lazy-load message access (direct seek by index, O(1) per record)
    //--------------------------------------------------------------------------

    uint32_t MeshDataStore::getDMMessageCount(uint32_t node_id) const
    {
        std::string path = getDMFilePath(node_id);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (file)
            fclose(file);
        return count;
    }

    bool MeshDataStore::hasDMMessages(uint32_t node_id) const
    {
        const MessageIndexEntry* entry = findIndexEntry(node_id, true);
        return entry && entry->message_count > 0;
    }

    bool MeshDataStore::getDMMessageByIndex(uint32_t node_id, uint32_t index, TextMessage& out) const
    {
        std::string path = getDMFilePath(node_id);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (!file || index >= count)
        {
            if (file)
                fclose(file);
            return false;
        }

        TextMessageRecord rec;
        bool ok = readRecordAt(file, index, rec);
        fclose(file);
        if (ok)
            recordToMsg(rec, out, true, 0);
        return ok;
    }

    uint32_t
    MeshDataStore::getDMMessageRange(uint32_t node_id, uint32_t start, uint32_t count, std::vector<TextMessage>& out) const
    {
        std::string path = getDMFilePath(node_id);
        uint32_t total = 0;
        FILE* file = openMessageFile(path.c_str(), total);
        if (!file || start >= total)
        {
            if (file)
                fclose(file);
            return 0;
        }

        uint32_t to_read = std::min(count, total - start);
        fseek(file, msgRecordOffset(start), SEEK_SET);

        uint32_t loaded = 0;
        for (uint32_t i = 0; i < to_read; i++)
        {
            TextMessageRecord rec;
            if (fread(&rec, sizeof(rec), 1, file) != 1)
                break;
            TextMessage msg;
            recordToMsg(rec, msg, true, 0);
            out.push_back(std::move(msg));
            loaded++;
        }

        fclose(file);
        return loaded;
    }

    uint32_t MeshDataStore::getDMTextLengths(uint32_t node_id, std::vector<uint16_t>& text_lengths) const
    {
        std::string path = getDMFilePath(node_id);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (!file)
            return 0;

        text_lengths.clear();
        text_lengths.reserve(count);

        for (uint32_t i = 0; i < count; i++)
        {
            long offset = msgRecordOffset(i) + offsetof(TextMessageRecord, text_len);
            fseek(file, offset, SEEK_SET);
            uint8_t tlen = 0;
            if (fread(&tlen, 1, 1, file) != 1)
                break;
            text_lengths.push_back(tlen);
        }

        fclose(file);
        return (uint32_t)text_lengths.size();
    }

    uint32_t MeshDataStore::forEachDMMessage(uint32_t node_id,
                                             std::function<bool(uint32_t index, const TextMessage& msg)> callback) const
    {
        std::string path = getDMFilePath(node_id);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (!file)
            return 0;

        fseek(file, msgRecordOffset(0), SEEK_SET);
        uint32_t iterated = 0;
        for (uint32_t i = 0; i < count; i++)
        {
            TextMessageRecord rec;
            if (fread(&rec, sizeof(rec), 1, file) != 1)
                break;
            TextMessage msg;
            recordToMsg(rec, msg, true, 0);
            iterated++;
            if (!callback(i, msg))
                break;
        }

        fclose(file);
        return iterated;
    }

    //--------------------------------------------------------------------------
    // Channel lazy-loading (mirrors DM methods)
    //--------------------------------------------------------------------------

    uint32_t MeshDataStore::getChannelMessageCount(uint8_t channel) const
    {
        std::string path = getChannelFilePath(channel);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (file)
            fclose(file);
        return count;
    }

    uint32_t
    MeshDataStore::getChannelMessageRange(uint8_t channel, uint32_t start, uint32_t count, std::vector<TextMessage>& out) const
    {
        std::string path = getChannelFilePath(channel);
        uint32_t total = 0;
        FILE* file = openMessageFile(path.c_str(), total);
        if (!file || start >= total)
        {
            if (file)
                fclose(file);
            return 0;
        }

        uint32_t to_read = std::min(count, total - start);
        fseek(file, msgRecordOffset(start), SEEK_SET);

        uint32_t loaded = 0;
        for (uint32_t i = 0; i < to_read; i++)
        {
            TextMessageRecord rec;
            if (fread(&rec, sizeof(rec), 1, file) != 1)
                break;
            TextMessage msg;
            recordToMsg(rec, msg, false, channel);
            out.push_back(std::move(msg));
            loaded++;
        }

        fclose(file);
        return loaded;
    }

    uint32_t MeshDataStore::forEachChannelMessage(uint8_t channel,
                                                  std::function<bool(uint32_t index, const TextMessage& msg)> callback) const
    {
        std::string path = getChannelFilePath(channel);
        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count);
        if (!file)
            return 0;

        fseek(file, msgRecordOffset(0), SEEK_SET);
        uint32_t iterated = 0;
        for (uint32_t i = 0; i < count; i++)
        {
            TextMessageRecord rec;
            if (fread(&rec, sizeof(rec), 1, file) != 1)
                break;
            TextMessage msg;
            recordToMsg(rec, msg, false, channel);
            iterated++;
            if (!callback(i, msg))
                break;
        }

        fclose(file);
        return iterated;
    }

    //--------------------------------------------------------------------------
    // Index Management
    //--------------------------------------------------------------------------

    bool MeshDataStore::loadIndex()
    {
        FILE* file = fopen(MSG_INDEX_FILE, "rb");
        if (!file)
        {
            ESP_LOGW(TAG, "No message index file found");
            return false;
        }

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, sizeof(magic), 1, file) != 1 || fread(&version, sizeof(version), 1, file) != 1 ||
            fread(&count, sizeof(count), 1, file) != 1)
        {
            fclose(file);
            return false;
        }

        if (magic != MSG_INDEX_MAGIC || version != MSG_FILE_VERSION)
        {
            ESP_LOGW(TAG, "Invalid message index header");
            fclose(file);
            return false;
        }

        _message_index.clear();
        _message_index.reserve(count);

        for (uint32_t i = 0; i < count; i++)
        {
            MessageIndexEntry entry = {};

            if (fread(&entry.node_id, sizeof(entry.node_id), 1, file) != 1)
                break;
            if (fread(&entry.channel, sizeof(entry.channel), 1, file) != 1)
                break;

            uint8_t flags = 0;
            if (fread(&flags, sizeof(flags), 1, file) != 1)
                break;
            entry.is_direct = (flags & 0x01) != 0;

            if (fread(&entry.message_count, sizeof(entry.message_count), 1, file) != 1)
                break;
            if (fread(&entry.unread_count, sizeof(entry.unread_count), 1, file) != 1)
                break;
            if (fread(&entry.last_timestamp, sizeof(entry.last_timestamp), 1, file) != 1)
                break;

            _message_index.push_back(entry);
        }

        fclose(file);
        ESP_LOGD(TAG, "Loaded message index with %d entries", _message_index.size());
        return true;
    }

    bool MeshDataStore::saveIndex()
    {
        FILE* file = fopen(MSG_INDEX_FILE, "wb");
        if (!file)
        {
            ESP_LOGE(TAG, "Failed to open %s for writing", MSG_INDEX_FILE);
            return false;
        }

        uint32_t magic = MSG_INDEX_MAGIC;
        uint32_t version = MSG_FILE_VERSION;
        uint32_t count = _message_index.size();

        fwrite(&magic, sizeof(magic), 1, file);
        fwrite(&version, sizeof(version), 1, file);
        fwrite(&count, sizeof(count), 1, file);

        for (const auto& entry : _message_index)
        {
            fwrite(&entry.node_id, sizeof(entry.node_id), 1, file);
            fwrite(&entry.channel, sizeof(entry.channel), 1, file);

            uint8_t flags = entry.is_direct ? 0x01 : 0;
            fwrite(&flags, sizeof(flags), 1, file);

            fwrite(&entry.message_count, sizeof(entry.message_count), 1, file);
            fwrite(&entry.unread_count, sizeof(entry.unread_count), 1, file);
            fwrite(&entry.last_timestamp, sizeof(entry.last_timestamp), 1, file);
        }

        fclose(file);
        ESP_LOGD(TAG, "Saved message index with %d entries", _message_index.size());
        return true;
    }

    static bool scanMessageFileForIndex(
        const char* path, uint32_t filter_node_id, bool is_dm, uint32_t& out_count, uint32_t& out_unread, uint32_t& out_last_ts)
    {
        out_count = 0;
        out_unread = 0;
        out_last_ts = 0;

        uint32_t count = 0;
        FILE* file = openMessageFile(path, count);
        if (!file || count == 0)
        {
            if (file)
                fclose(file);
            return false;
        }

        out_count = count;

        for (uint32_t i = 0; i < count; i++)
        {
            TextMessageRecord rec;
            if (!readRecordAt(file, i, rec))
                break;

            bool is_read = (rec.status & 0x80) != 0;
            if (!is_read)
            {
                if (!is_dm || rec.from == filter_node_id)
                    out_unread++;
            }

            if (rec.timestamp > out_last_ts)
                out_last_ts = rec.timestamp;
        }

        fclose(file);
        return true;
    }

    bool MeshDataStore::rebuildIndex()
    {
        ESP_LOGD(TAG, "Rebuilding message index from files...");
        _message_index.clear();

        DIR* dir = opendir(MESSAGES_DIR);
        if (!dir)
        {
            ESP_LOGW(TAG, "Cannot open messages directory");
            return false;
        }

        struct dirent* entry;
        while ((entry = readdir(dir)) != nullptr)
        {
            std::string filename(entry->d_name);

            if (filename.length() == 12 && filename.substr(8) == ".msg")
            {
                uint32_t node_id = 0;
                if (sscanf(filename.c_str(), "%08lx", (unsigned long*)&node_id) == 1)
                {
                    std::string path = getDMFilePath(node_id);
                    uint32_t count = 0, unread = 0, last_ts = 0;
                    if (scanMessageFileForIndex(path.c_str(), node_id, true, count, unread, last_ts))
                    {
                        MessageIndexEntry idx = {};
                        idx.node_id = node_id;
                        idx.is_direct = true;
                        idx.message_count = count;
                        idx.unread_count = unread;
                        idx.last_timestamp = last_ts;
                        _message_index.push_back(idx);
                    }
                }
            }
            else if (filename.length() >= 7 && filename.substr(0, 3) == "ch_")
            {
                int channel = -1;
                if (sscanf(filename.c_str(), "ch_%d.msg", &channel) == 1 && channel >= 0 && channel < 8)
                {
                    std::string path = getChannelFilePath((uint8_t)channel);
                    uint32_t count = 0, unread = 0, last_ts = 0;
                    if (scanMessageFileForIndex(path.c_str(), 0, false, count, unread, last_ts))
                    {
                        MessageIndexEntry idx = {};
                        idx.node_id = 0;
                        idx.channel = (uint8_t)channel;
                        idx.is_direct = false;
                        idx.message_count = count;
                        idx.unread_count = unread;
                        idx.last_timestamp = last_ts;
                        _message_index.push_back(idx);
                    }
                }
            }
        }

        closedir(dir);
        saveIndex();

        ESP_LOGD(TAG, "Rebuilt message index with %d conversations", _message_index.size());
        return true;
    }

    MessageIndexEntry* MeshDataStore::findIndexEntry(uint32_t node_id, bool is_direct)
    {
        for (auto& entry : _message_index)
        {
            if (is_direct)
            {
                if (entry.is_direct && entry.node_id == node_id)
                    return &entry;
            }
            else
            {
                if (!entry.is_direct && entry.channel == (uint8_t)node_id)
                    return &entry;
            }
        }
        return nullptr;
    }

    const MessageIndexEntry* MeshDataStore::findIndexEntry(uint32_t node_id, bool is_direct) const
    {
        for (const auto& entry : _message_index)
        {
            if (is_direct)
            {
                if (entry.is_direct && entry.node_id == node_id)
                    return &entry;
            }
            else
            {
                if (!entry.is_direct && entry.channel == (uint8_t)node_id)
                    return &entry;
            }
        }
        return nullptr;
    }

    void MeshDataStore::updateIndexEntry(
        uint32_t node_id, bool is_direct, uint8_t channel, int count_delta, int unread_delta, uint32_t timestamp)
    {
        MessageIndexEntry* entry = findIndexEntry(is_direct ? node_id : channel, is_direct);

        if (entry)
        {
            entry->message_count += count_delta;
            entry->unread_count += unread_delta;
            if (entry->unread_count > entry->message_count)
                entry->unread_count = entry->message_count;
            if (timestamp > entry->last_timestamp)
                entry->last_timestamp = timestamp;
        }
        else
        {
            MessageIndexEntry new_entry = {};
            new_entry.node_id = is_direct ? node_id : 0;
            new_entry.channel = channel;
            new_entry.is_direct = is_direct;
            new_entry.message_count = (count_delta > 0) ? count_delta : 0;
            new_entry.unread_count = (unread_delta > 0) ? unread_delta : 0;
            new_entry.last_timestamp = timestamp;
            _message_index.push_back(new_entry);
        }

        saveIndex();
    }

    //--------------------------------------------------------------------------
    // Public Message API
    //--------------------------------------------------------------------------

    void MeshDataStore::addMessage(const TextMessage& msg)
    {
        if (!_initialized)
            init();

        std::string path;
        uint32_t index_id;

        if (msg.is_direct)
        {
            index_id = (msg.from != 0 && msg.to != 0) ? (msg.read ? msg.to : msg.from) : (msg.from != 0 ? msg.from : msg.to);
            path = getDMFilePath(index_id);
            ESP_LOGI(TAG,
                     "Adding DM: from=0x%08lX to=0x%08lX -> file for node 0x%08lX",
                     (unsigned long)msg.from,
                     (unsigned long)msg.to,
                     (unsigned long)index_id);
        }
        else
        {
            index_id = msg.channel;
            path = getChannelFilePath(msg.channel);
            ESP_LOGI(TAG, "Adding channel message: channel=%d", msg.channel);
        }

        if (appendMessageToFile(path, msg))
        {
            int unread_delta = msg.read ? 0 : 1;
            updateIndexEntry(index_id, msg.is_direct, msg.channel, 1, unread_delta, msg.timestamp);
            _change_counter++;

            if (msg.read)
                _stats.messages_sent++;
            else
                _stats.messages_received++;

            ESP_LOGD(TAG, "Message saved to %s (unread=%d)", path.c_str(), !msg.read);
        }
        else
        {
            ESP_LOGE(TAG, "Failed to save message to %s", path.c_str());
        }
    }

    std::vector<TextMessage> MeshDataStore::getDirectMessages(uint32_t node_id) const
    {
        std::vector<TextMessage> messages;
        getDMMessageRange(node_id, 0, UINT32_MAX, messages);
        return messages;
    }

    std::vector<TextMessage> MeshDataStore::getChannelMessages(uint8_t channel) const
    {
        std::vector<TextMessage> messages;
        getChannelMessageRange(channel, 0, UINT32_MAX, messages);
        return messages;
    }

    uint32_t MeshDataStore::getUnreadDMCount(uint32_t node_id) const
    {
        const MessageIndexEntry* entry = findIndexEntry(node_id, true);
        return entry ? entry->unread_count : 0;
    }

    uint32_t MeshDataStore::getUnreadChannelCount(uint8_t channel) const
    {
        const MessageIndexEntry* entry = findIndexEntry(channel, false);
        return entry ? entry->unread_count : 0;
    }

    void MeshDataStore::markMessagesRead(uint32_t id, bool is_channel)
    {
        std::string path = is_channel ? getChannelFilePath((uint8_t)id) : getDMFilePath(id);

        uint32_t count = 0;
        FILE* file = openMessageFile(path.c_str(), count, "r+b");
        if (!file)
            return;

        bool changed = false;
        for (uint32_t i = 0; i < count; i++)
        {
            long status_off = msgRecordOffset(i) + offsetof(TextMessageRecord, status);
            fseek(file, status_off, SEEK_SET);

            uint8_t st = 0;
            if (fread(&st, 1, 1, file) != 1)
                break;

            if (!(st & 0x80))
            {
                st |= 0x80;
                fseek(file, status_off, SEEK_SET);
                fwrite(&st, 1, 1, file);
                changed = true;
            }
        }

        if (changed)
            fflush(file);
        fclose(file);

        if (changed)
        {
            MessageIndexEntry* entry = findIndexEntry(id, !is_channel);
            if (entry)
            {
                entry->unread_count = 0;
                saveIndex();
            }
        }
    }

    void MeshDataStore::clearMessages()
    {
        DIR* dir = opendir(MESSAGES_DIR);
        if (dir)
        {
            struct dirent* entry;
            while ((entry = readdir(dir)) != nullptr)
            {
                std::string filename(entry->d_name);
                if (filename.find(".msg") != std::string::npos)
                {
                    std::string path = std::string(MESSAGES_DIR) + "/" + filename;
                    remove(path.c_str());
                }
            }
            closedir(dir);
        }

        _message_index.clear();
        saveIndex();
        _change_counter++;

        ESP_LOGI(TAG, "Cleared all messages");
    }

    void MeshDataStore::clearConversation(uint32_t node_id, bool is_channel)
    {
        std::string path;
        if (is_channel)
            path = getChannelFilePath((uint8_t)node_id);
        else
            path = getDMFilePath(node_id);

        remove(path.c_str());

        auto it = std::find_if(_message_index.begin(),
                               _message_index.end(),
                               [node_id, is_channel](const MessageIndexEntry& e)
                               {
                                   if (is_channel)
                                       return !e.is_direct && e.channel == (uint8_t)node_id;
                                   return e.is_direct && e.node_id == node_id;
                               });

        if (it != _message_index.end())
        {
            _message_index.erase(it);
            saveIndex();
        }
        _change_counter++;

        ESP_LOGI(TAG, "Cleared conversation: %s", path.c_str());
    }

    std::vector<uint32_t> MeshDataStore::getConversationsWithUnread() const
    {
        std::vector<uint32_t> result;
        for (const auto& entry : _message_index)
        {
            if (entry.is_direct && entry.unread_count > 0)
                result.push_back(entry.node_id);
        }
        return result;
    }

    uint32_t MeshDataStore::getTotalUnreadDMCount() const
    {
        uint32_t total = 0;
        for (const auto& entry : _message_index)
        {
            if (entry.is_direct)
                total += entry.unread_count;
        }
        return total;
    }

    uint32_t MeshDataStore::getTotalUnreadChannelCount() const
    {
        uint32_t total = 0;
        for (const auto& entry : _message_index)
        {
            if (!entry.is_direct)
                total += entry.unread_count;
        }
        return total;
    }

    //--------------------------------------------------------------------------
    // Packet Log
    //--------------------------------------------------------------------------

    void MeshDataStore::addPacketLogEntry(const PacketLogEntry& entry)
    {
        _packet_log.push(entry);
        if (entry.is_tx)
        {
            _stats.tx_packets++;
            return;
        }
        _stats.rx_packets++;
        _port_dist.rx_total++;

        if (entry.crc_error)
        {
            _port_dist.crc_errors++;
            return;
        }

        for (int i = 0; i < _port_dist.count; i++)
        {
            if (_port_dist.entries[i].port == entry.port)
            {
                _port_dist.entries[i].rx_count++;
                return;
            }
        }
        if (_port_dist.count < PORT_STATS_MAX)
        {
            _port_dist.entries[_port_dist.count] = {entry.port, 1};
            _port_dist.count++;
        }
    }

    //--------------------------------------------------------------------------
    // Node data removal
    //--------------------------------------------------------------------------

    void MeshDataStore::removeNodeData(uint32_t node_id)
    {
        std::string dm_path = getDMFilePath(node_id);
        if (remove(dm_path.c_str()) == 0)
            ESP_LOGI(TAG, "Removed DM file for 0x%08lX", (unsigned long)node_id);

        std::string tr_path = getTraceRouteFilePath(node_id);
        if (remove(tr_path.c_str()) == 0)
            ESP_LOGI(TAG, "Removed traceroute file for 0x%08lX", (unsigned long)node_id);
    }

    // Statistics
    //--------------------------------------------------------------------------

    void MeshDataStore::resetStats()
    {
        bool ble_state = _stats.ble_connected;
        _stats = MeshStats();
        _stats.ble_connected = ble_state;
    }

    //--------------------------------------------------------------------------
    // Graph Data
    //--------------------------------------------------------------------------

    void MeshDataStore::addBatteryPoint(float voltage)
    {
        GraphPoint point;
        point.timestamp_ms = (uint32_t)millis();
        point.value = voltage;
        _battery_history.push_back(point);

        while (_battery_history.size() > MAX_GRAPH_POINTS)
            _battery_history.erase(_battery_history.begin());
    }

    void MeshDataStore::addChannelActivityPoint(float packets_per_min)
    {
        GraphPoint point;
        point.timestamp_ms = (uint32_t)millis();
        point.value = packets_per_min;
        _channel_activity.push_back(point);

        while (_channel_activity.size() > MAX_GRAPH_POINTS)
            _channel_activity.erase(_channel_activity.begin());
    }

    void MeshDataStore::addRssiPoint(uint32_t node_id, int16_t rssi)
    {
        GraphPoint point;
        point.timestamp_ms = (uint32_t)millis();
        point.value = (float)rssi;

        auto& history = _rssi_history[node_id];
        history.push_back(point);

        while (history.size() > MAX_GRAPH_POINTS)
            history.erase(history.begin());
    }

    std::vector<GraphPoint> MeshDataStore::getRssiHistory(uint32_t node_id) const
    {
        auto it = _rssi_history.find(node_id);
        if (it != _rssi_history.end())
            return it->second;
        return {};
    }

    //--------------------------------------------------------------------------
    // Traceroute File Storage
    //--------------------------------------------------------------------------

    void MeshDataStore::trResultToRecord(const TraceRouteResult& src, TraceRouteRecord& dst)
    {
        memset(&dst, 0, sizeof(dst));
        dst.target_node_id = src.target_node_id;
        dst.timestamp = src.timestamp;
        dst.duration_sec = src.duration_sec;
        dst.status = static_cast<uint8_t>(src.status);
        dst.dest_snr_q4 = (int8_t)(src.dest_snr * 4.0f);
        dst.origin_snr_q4 = (int8_t)(src.origin_snr * 4.0f);
        dst.route_to_count = std::min((size_t)src.route_to.size(), (size_t)8);
        dst.route_back_count = std::min((size_t)src.route_back.size(), (size_t)8);
        for (uint8_t i = 0; i < dst.route_to_count; i++)
        {
            dst.route_to[i].node_id = src.route_to[i].node_id;
            dst.route_to[i].snr_q4 = (int8_t)(src.route_to[i].snr * 4.0f);
        }
        for (uint8_t i = 0; i < dst.route_back_count; i++)
        {
            dst.route_back[i].node_id = src.route_back[i].node_id;
            dst.route_back[i].snr_q4 = (int8_t)(src.route_back[i].snr * 4.0f);
        }
    }

    void MeshDataStore::trRecordToResult(const TraceRouteRecord& src, TraceRouteResult& dst)
    {
        dst.target_node_id = src.target_node_id;
        dst.timestamp = src.timestamp;
        dst.duration_sec = src.duration_sec;
        dst.status = static_cast<TraceRouteResult::Status>(src.status);
        dst.dest_snr = (float)src.dest_snr_q4 / 4.0f;
        dst.origin_snr = (float)src.origin_snr_q4 / 4.0f;
        dst.route_to.clear();
        dst.route_back.clear();
        for (uint8_t i = 0; i < src.route_to_count && i < 8; i++)
            dst.route_to.push_back({src.route_to[i].node_id, (float)src.route_to[i].snr_q4 / 4.0f});
        for (uint8_t i = 0; i < src.route_back_count && i < 8; i++)
            dst.route_back.push_back({src.route_back[i].node_id, (float)src.route_back[i].snr_q4 / 4.0f});
    }

    uint32_t MeshDataStore::getTraceRouteCount(uint32_t node_id) const
    {
        std::string path = getTraceRouteFilePath(node_id);
        FILE* file = fopen(path.c_str(), "rb");
        if (!file)
            return 0;

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&count, 4, 1, file) != 1)
        {
            fclose(file);
            return 0;
        }
        fclose(file);

        if (magic != TRC_FILE_MAGIC || version != TRC_FILE_VERSION)
            return 0;
        return count;
    }

    bool MeshDataStore::getTraceRouteByIndex(uint32_t node_id, uint32_t index, TraceRouteResult& out) const
    {
        std::string path = getTraceRouteFilePath(node_id);
        FILE* file = fopen(path.c_str(), "rb");
        if (!file)
            return false;

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&count, 4, 1, file) != 1 ||
            magic != TRC_FILE_MAGIC || version != TRC_FILE_VERSION || index >= count)
        {
            fclose(file);
            return false;
        }

        long offset = (long)TRC_FILE_HEADER_SIZE + (long)index * (long)sizeof(TraceRouteRecord);
        if (fseek(file, offset, SEEK_SET) != 0)
        {
            fclose(file);
            return false;
        }

        TraceRouteRecord rec;
        if (fread(&rec, sizeof(rec), 1, file) != 1)
        {
            fclose(file);
            return false;
        }
        fclose(file);
        trRecordToResult(rec, out);
        return true;
    }

    uint32_t MeshDataStore::getTraceRouteRange(uint32_t node_id,
                                               uint32_t start,
                                               uint32_t count,
                                               std::vector<TraceRouteResult>& out) const
    {
        std::string path = getTraceRouteFilePath(node_id);
        FILE* file = fopen(path.c_str(), "rb");
        if (!file)
            return 0;

        uint32_t magic = 0, version = 0, total = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&total, 4, 1, file) != 1 ||
            magic != TRC_FILE_MAGIC || version != TRC_FILE_VERSION || start >= total)
        {
            fclose(file);
            return 0;
        }

        uint32_t avail = total - start;
        uint32_t to_read = std::min(count, avail);

        long offset = (long)TRC_FILE_HEADER_SIZE + (long)start * (long)sizeof(TraceRouteRecord);
        if (fseek(file, offset, SEEK_SET) != 0)
        {
            fclose(file);
            return 0;
        }

        uint32_t loaded = 0;
        for (uint32_t i = 0; i < to_read; i++)
        {
            TraceRouteRecord rec;
            if (fread(&rec, sizeof(rec), 1, file) != 1)
                break;
            TraceRouteResult result;
            trRecordToResult(rec, result);
            out.push_back(std::move(result));
            loaded++;
        }
        fclose(file);
        return loaded;
    }

    uint32_t MeshDataStore::addTraceRoute(uint32_t node_id, const TraceRouteResult& result)
    {
        std::string path = getTraceRouteFilePath(node_id);

        std::vector<TraceRouteRecord> records;
        FILE* file = fopen(path.c_str(), "rb");
        if (file)
        {
            uint32_t magic = 0, version = 0, count = 0;
            if (fread(&magic, 4, 1, file) == 1 && fread(&version, 4, 1, file) == 1 && fread(&count, 4, 1, file) == 1 &&
                magic == TRC_FILE_MAGIC && version == TRC_FILE_VERSION)
            {
                records.resize(count);
                for (uint32_t i = 0; i < count; i++)
                {
                    if (fread(&records[i], sizeof(TraceRouteRecord), 1, file) != 1)
                    {
                        records.resize(i);
                        break;
                    }
                }
            }
            fclose(file);
        }

        TraceRouteRecord rec;
        trResultToRecord(result, rec);
        records.push_back(rec);

        while (records.size() > MAX_TRACEROUTES_PER_NODE)
            records.erase(records.begin());

        uint32_t new_index = (uint32_t)(records.size() - 1);

        file = fopen(path.c_str(), "wb");
        if (!file)
        {
            ESP_LOGE(TAG, "Failed to write traceroute file: %s", path.c_str());
            return new_index;
        }

        uint32_t magic = TRC_FILE_MAGIC;
        uint32_t version = TRC_FILE_VERSION;
        uint32_t count = (uint32_t)records.size();
        fwrite(&magic, 4, 1, file);
        fwrite(&version, 4, 1, file);
        fwrite(&count, 4, 1, file);
        for (const auto& r : records)
            fwrite(&r, sizeof(r), 1, file);
        fclose(file);

        ESP_LOGD(TAG, "Saved %lu traceroute records for node %08lx", (unsigned long)count, (unsigned long)node_id);
        return new_index;
    }

    bool MeshDataStore::updateTraceRoute(uint32_t node_id, uint32_t index, const TraceRouteResult& result)
    {
        std::string path = getTraceRouteFilePath(node_id);
        FILE* file = fopen(path.c_str(), "r+b");
        if (!file)
            return false;

        uint32_t magic = 0, version = 0, count = 0;
        if (fread(&magic, 4, 1, file) != 1 || fread(&version, 4, 1, file) != 1 || fread(&count, 4, 1, file) != 1 ||
            magic != TRC_FILE_MAGIC || version != TRC_FILE_VERSION || index >= count)
        {
            fclose(file);
            return false;
        }

        TraceRouteRecord rec;
        trResultToRecord(result, rec);

        long offset = (long)TRC_FILE_HEADER_SIZE + (long)index * (long)sizeof(TraceRouteRecord);
        if (fseek(file, offset, SEEK_SET) != 0)
        {
            fclose(file);
            return false;
        }

        bool ok = fwrite(&rec, sizeof(rec), 1, file) == 1;
        fclose(file);
        return ok;
    }

    //--------------------------------------------------------------------------
    // Message templates
    //--------------------------------------------------------------------------

    std::vector<std::string> load_message_templates()
    {
        static const std::vector<std::string> defaults = {"Hi \U0001F600",
                                                          "How do u do?",
                                                          "Good night \U0001F4AB",
                                                          "Bye bye \U0001F60E"};

        FILE* f = fopen(TEMPLATES_FILE, "r");
        if (!f)
        {
            f = fopen(TEMPLATES_FILE, "w");
            if (f)
            {
                for (const auto& t : defaults)
                    fprintf(f, "%s\n", t.c_str());
                fclose(f);
            }
            return defaults;
        }

        std::vector<std::string> templates;
        char line[256];
        while (fgets(line, sizeof(line), f))
        {
            size_t len = strlen(line);
            while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
                line[--len] = '\0';
            if (len > 0)
                templates.push_back(line);
        }
        fclose(f);

        return templates.empty() ? defaults : templates;
    }

} // namespace Mesh
