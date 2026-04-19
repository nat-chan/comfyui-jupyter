"""Allow running as `python -m comfyui_kernel`."""

from ipykernel.kernelapp import IPKernelApp

from comfyui_kernel.kernel import ComfyUIKernel


class ComfyUIKernelApp(IPKernelApp):  # type: ignore[misc]
    kernel_class = ComfyUIKernel


if __name__ == "__main__":
    ComfyUIKernelApp.launch_instance()
