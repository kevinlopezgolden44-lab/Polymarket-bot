import unittest
from your_module import detect_category  # Replace with actual import

class TestCategoryDetection(unittest.TestCase):
    
    def test_category_detection(self):
        test_cases = {
            'Will it rain tomorrow?': 'Weather',  # Example category
            'Who will win the next election?': 'Politics',
            'What is the capital of France?': 'Geography',
            'Is Tesla a good investment?': 'Economics',
            'What is the purpose of life?': 'Philosophy',
            'Will a new iPhone be released this year?': 'Technology',
            'Are aliens real?': 'Science',
            'Who will win the football match?': 'Sports',
        }
        
        for question, expected_category in test_cases.items():
            with self.subTest(question=question):
                self.assertEqual(detect_category(question), expected_category)

if __name__ == '__main__':
    unittest.main()