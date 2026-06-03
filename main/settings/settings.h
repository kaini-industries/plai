/**
 * @file settings.h
 * @brief Settings management system with NVS caching
 */

#pragma once

#include <string>
#include <vector>
#include <functional>
#include <unordered_map>
#include "nvs_flash.h"
#include "esp_log.h"

namespace HAL
{
    class Hal;
}

namespace SETTINGS
{

    enum SettingType
    {
        TYPE_NONE,
        TYPE_BOOL,
        TYPE_NUMBER,
        TYPE_STRING,
        TYPE_CALLBACK
    };

    struct SettingItem_t
    {
        // Immutable fields point directly at string literals (stored in flash/.rodata),
        // so they cost no heap. Only `value` is mutable and stays a std::string.
        const char* key;
        const char* label;
        SettingType type;
        const char* default_val;
        std::string value;
        const char* min_val; // For TYPE_NUMBER
        const char* max_val; // For TYPE_NUMBER
        const char* hint;
        std::function<void(SettingItem_t& item)> callback = {};
        // Optional conditional visibility: item is only shown when the item identified by
        // visible_when_key (in the same group) currently equals visible_when_value.
        // Leave visible_when_key null/empty to always show the item.
        const char* visible_when_key = nullptr;
        const char* visible_when_value = nullptr;
    };

    struct SettingGroup_t
    {
        std::string name;
        std::string nvs_namespace;
        std::vector<SettingItem_t> items;
        std::function<void(SettingGroup_t& group)> callback;
    };

    class Settings
    {
    public:
        Settings();
        ~Settings();

        /**
         * @brief Set HAL pointer and initialize callbacks that require HAL access
         * @param hal Pointer to HAL instance
         */
        void setHal(HAL::Hal* hal);

        /**
         * @brief Initialize settings system and load values from NVS
         * @return true if successful
         */
        bool init();

        /**
         * @brief Get all setting groups metadata
         * @return Vector of setting groups
         */
        std::vector<SettingGroup_t> getMetadata() const;

        /**
         * @brief Get a reference to the live setting groups metadata (no copy).
         *
         * The settings UI uses SettingItem_t::value only as per-frame scratch (it is
         * refreshed from the cache on every render), so it can safely operate on the
         * canonical metadata directly. Prefer this over getMetadata() to avoid holding
         * a second full copy of the metadata (~25 KB) in internal RAM.
         * @return Reference to the internal metadata vector
         */
        std::vector<SettingGroup_t>& getMetadataRef();

        /**
         * @brief Get boolean setting value
         * @param ns Namespace
         * @param key Setting key
         * @return Boolean value
         */
        bool getBool(const std::string& ns, const std::string& key);

        /**
         * @brief Get number setting value
         * @param ns Namespace
         * @param key Setting key
         * @return Integer value
         */
        int32_t getNumber(const std::string& ns, const std::string& key);

        /**
         * @brief Get string setting value
         * @param ns Namespace
         * @param key Setting key
         * @return String value
         */
        std::string getString(const std::string& ns, const std::string& key);

        /**
         * @brief Set boolean setting value
         * @param ns Namespace
         * @param key Setting key
         * @param value Boolean value
         * @return true if successful
         */
        bool setBool(const std::string& ns, const std::string& key, bool value);

        /**
         * @brief Set number setting value
         * @param ns Namespace
         * @param key Setting key
         * @param value Integer value
         * @return true if successful
         */
        bool setNumber(const std::string& ns, const std::string& key, int32_t value);

        /**
         * @brief Set string setting value
         * @param ns Namespace
         * @param key Setting key
         * @param value String value
         * @return true if successful
         */
        bool setString(const std::string& ns, const std::string& key, const std::string& value);

        /**
         * @brief Save all modified settings to NVS
         * @return true if successful
         */
        bool saveAll();

        /**
         * @brief Export all settings to a file
         * @param filename The name of the file to export to
         * @return true if successful
         */
        bool exportToFile(const std::string& filename) const;

        /**
         * @brief Import settings from a file
         * @param filename The name of the file to import from
         * @return true if successful
         */
        bool importFromFile(const std::string& filename);

        /**
         * @brief Apply current mesh-related settings to MeshService
         * Reads LoRa, Node info, Security, and Position settings and pushes to mesh
         */
        void applyMeshConfig(SettingItem_t& item);

        /**
         * @brief Translate a human-readable timezone label (e.g. "GMT+2") to a POSIX TZ
         *        string and apply it to the system environment via setenv/tzset.
         * @param tz Timezone label (see system settings timezone option list)
         */
        static void applyTimezone(const std::string& tz);

    private:
        static const char* const NVS_PARTITIONS[];
        const char* _active_partition = nullptr;

        // Cache storage
        struct CachedValue
        {
            SettingType type;
            union
            {
                bool bool_val;
                int32_t num_val;
            };
            std::string str_val;
        };

        HAL::Hal* _hal = nullptr;
        std::unordered_map<std::string, CachedValue> _cache;
        std::vector<SettingGroup_t> _metadata;
        bool _initialized = false;

        bool _initNvs();
        void _deinitNvs();
        void _loadSettings();
        std::string _makeKey(const std::string& ns, const std::string& key) const;
        const SettingItem_t* _findItem(const std::string& ns, const std::string& key) const;
    };

} // namespace SETTINGS