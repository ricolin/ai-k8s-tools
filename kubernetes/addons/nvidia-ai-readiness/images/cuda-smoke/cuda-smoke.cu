#include <cuda_runtime.h>

#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

static void check(cudaError_t result, const char* operation) {
  if (result != cudaSuccess) {
    std::cerr << operation << ": " << cudaGetErrorString(result) << std::endl;
    std::exit(1);
  }
}

static std::string uuid_string(const cudaUUID_t& uuid) {
  std::ostringstream output;
  output << "GPU-" << std::hex << std::setfill('0');
  for (int index = 0; index < 16; ++index) {
    if (index == 4 || index == 6 || index == 8 || index == 10) output << '-';
    output << std::setw(2)
           << static_cast<unsigned int>(
                  static_cast<unsigned char>(uuid.bytes[index]));
  }
  return output.str();
}

int main() {
  int count = 0;
  check(cudaGetDeviceCount(&count), "cudaGetDeviceCount");
  const char* expected_text = std::getenv("EXPECTED_GPU_COUNT");
  const int expected = expected_text ? std::stoi(expected_text) : count;
  if (count != expected) {
    std::cerr << "expected " << expected << " CUDA devices, observed " << count << std::endl;
    return 1;
  }

  std::cout << "{\"status\":\"PASS\",\"device_count\":" << count << ",\"devices\":[";
  for (int index = 0; index < count; ++index) {
    cudaDeviceProp properties{};
    cudaUUID_t uuid{};
    check(cudaGetDeviceProperties(&properties, index), "cudaGetDeviceProperties");
    check(cudaDeviceGetUuid(&uuid, index), "cudaDeviceGetUuid");
    int* value = nullptr;
    check(cudaSetDevice(index), "cudaSetDevice");
    check(cudaMalloc(&value, sizeof(int)), "cudaMalloc");
    check(cudaMemset(value, index + 1, sizeof(int)), "cudaMemset");
    check(cudaDeviceSynchronize(), "cudaDeviceSynchronize");
    check(cudaFree(value), "cudaFree");
    if (index) std::cout << ',';
    std::cout << "{\"index\":" << index << ",\"name\":\"" << properties.name
              << "\",\"uuid\":\"" << uuid_string(uuid)
              << "\",\"compute_capability\":\"" << properties.major << '.'
              << properties.minor << "\",\"memory_bytes\":"
              << properties.totalGlobalMem << '}';
  }
  std::cout << "]}" << std::endl;
  return 0;
}
