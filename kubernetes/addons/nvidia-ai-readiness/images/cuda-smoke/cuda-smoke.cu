#include <cuda_runtime.h>

#include <cstdlib>
#include <iostream>
#include <string>

static void check(cudaError_t result, const char* operation) {
  if (result != cudaSuccess) {
    std::cerr << operation << ": " << cudaGetErrorString(result) << std::endl;
    std::exit(1);
  }
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
    check(cudaGetDeviceProperties(&properties, index), "cudaGetDeviceProperties");
    int* value = nullptr;
    check(cudaSetDevice(index), "cudaSetDevice");
    check(cudaMalloc(&value, sizeof(int)), "cudaMalloc");
    check(cudaMemset(value, index + 1, sizeof(int)), "cudaMemset");
    check(cudaDeviceSynchronize(), "cudaDeviceSynchronize");
    check(cudaFree(value), "cudaFree");
    if (index) std::cout << ',';
    std::cout << "{\"index\":" << index << ",\"name\":\"" << properties.name
              << "\",\"compute_capability\":\"" << properties.major << '.'
              << properties.minor << "\",\"memory_bytes\":"
              << properties.totalGlobalMem << '}';
  }
  std::cout << "]}" << std::endl;
  return 0;
}
