def greet(name):
	"""Say hello to someone."""
	message = f'Hello, {name}!'
	print(message)


def main():
	"""Main entry point."""
	names = ['Alice', 'Bob', 'Charlie']
	for name in names:
		greet(name)


if __name__ == '__main__':
	main()
