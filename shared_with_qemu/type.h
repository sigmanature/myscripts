/* -------- 临时把缺失类型映射成 u64，避免编译报错 ------------ */
typedef unsigned long long dev_t;
typedef unsigned long long sector_t;